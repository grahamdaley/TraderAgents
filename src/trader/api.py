from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from trader.broker import get_broker
from trader.config import get_settings
from trader.db import get_session_factory, init_db
from trader.nous import NousCatalogError, verify_configured_models
from trader.schemas import ApprovalRequest
from trader import store
from trader.services import get_book_snapshot, resume_with_decision, start_research_run

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)


async def get_session():
    factory = get_session_factory()
    async with factory() as session:
        yield session


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_db()
    try:
        verify_configured_models(settings)
    except NousCatalogError as exc:
        if settings.allow_mock_llm or settings.skip_nous_catalog_check:
            logger.warning("Nous catalog check skipped/failed: %s", exc)
        else:
            logger.error("Nous catalog check failed: %s", exc)
            raise
    try:
        get_broker()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Broker init warning: %s", exc)
    try:
        from trader.graph import get_compiled_graph

        get_compiled_graph()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Graph init warning: %s", exc)
    yield


app = FastAPI(title="Research Desk", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class StartRunRequest(BaseModel):
    ticker: str = Field(min_length=1, max_length=16)


@app.get("/api/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "ok": True,
        "trading_mode": settings.ibkr_trading_mode,
        "live_orders_enabled": settings.ibkr_live_orders_enabled,
        "mock_llm": settings.allow_mock_llm or not bool(settings.nous_api_key),
        "mock_broker": get_broker().using_mock,
    }


@app.get("/api/models")
async def models() -> dict[str, str]:
    return get_settings().model_map()


@app.post("/api/runs")
async def create_run(
    body: StartRunRequest, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    ticker = body.ticker.strip().upper()
    if not ticker.isalnum():
        raise HTTPException(400, "ticker must be alphanumeric")
    try:
        return await start_research_run(session, ticker)
    except Exception as exc:  # noqa: BLE001
        logger.exception("create_run failed")
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/runs")
async def runs(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = await store.list_runs(session)
    return [
        {
            "id": r.id,
            "ticker": r.ticker,
            "status": r.status,
            "nodes": r.nodes,
            "error": r.error,
            "created_at": r.created_at,
            "updated_at": r.updated_at,
        }
        for r in rows
    ]


@app.get("/api/runs/{run_id}")
async def run_detail(
    run_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    row = await store.get_run(session, run_id)
    if not row:
        raise HTTPException(404, "run not found")
    return {
        "id": row.id,
        "ticker": row.ticker,
        "status": row.status,
        "nodes": row.nodes,
        "state": row.state_snapshot,
        "error": row.error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@app.get("/api/proposals")
async def proposals(
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    rows = await store.list_open_proposals(session)
    return [
        {
            "id": r.id,
            "run_id": r.run_id,
            "ticker": r.ticker,
            "status": r.status,
            "content_hash": r.content_hash,
            "expires_at": r.expires_at,
            "payload": r.payload,
            "created_at": r.created_at,
        }
        for r in rows
    ]


@app.get("/api/proposals/{proposal_id}")
async def proposal_detail(
    proposal_id: str, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    row = await store.get_proposal(session, proposal_id)
    if not row:
        raise HTTPException(404, "proposal not found")
    return {
        "id": row.id,
        "run_id": row.run_id,
        "ticker": row.ticker,
        "status": row.status,
        "content_hash": row.content_hash,
        "expires_at": row.expires_at,
        "payload": row.payload,
        "created_at": row.created_at,
    }


@app.post("/api/runs/{run_id}/decision")
async def decide(
    run_id: str,
    body: ApprovalRequest,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        return await resume_with_decision(session, run_id=run_id, request=body)
    except KeyError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        logger.exception("decision failed")
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/book")
async def book() -> dict[str, Any]:
    try:
        snap = get_book_snapshot()
        return snap.model_dump(mode="json")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"book unavailable: {exc}") from exc


@app.get("/api/fills")
async def fills(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = await store.list_fills(session)
    return [
        {
            "id": r.id,
            "intent_id": r.intent_id,
            "broker_order_id": r.broker_order_id,
            "ticker": r.ticker,
            "side": r.side,
            "quantity": r.quantity,
            "fill_price": r.fill_price,
            "account_id": r.account_id,
            "filled_at": r.filled_at,
        }
        for r in rows
    ]


@app.get("/api/audit")
async def audit(session: AsyncSession = Depends(get_session)) -> list[dict[str, Any]]:
    rows = await store.list_audit(session)
    return [
        {
            "id": r.id,
            "run_id": r.run_id,
            "event_type": r.event_type,
            "actor": r.actor,
            "payload": r.payload,
            "created_at": r.created_at,
        }
        for r in rows
    ]
