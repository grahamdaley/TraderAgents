from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from trader import store
from trader.broker import get_broker
from trader.config import get_settings
from trader.db import get_session_factory, init_db
from trader.autopilot import autopilot_armed, autopilot_loop
from trader.exits import exit_review_loop, review_exits
from trader.fills import fill_reconcile_loop, reconcile_submitted_fills
from trader.markets import normalize_ticker, parse_market
from trader.nous import NousCatalogError, verify_configured_models
from trader.positions import plan_to_dict
from trader.scanner import load_universe, run_scan
from trader.schemas import ApprovalRequest
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
        # Connect off the uvicorn loop — IB client owns its own thread/loop.
        await asyncio.to_thread(get_broker)
    except Exception as exc:  # noqa: BLE001
        if settings.allow_mock_broker:
            logger.warning("Broker init warning: %s", exc)
        else:
            logger.error("Broker init failed (ALLOW_MOCK_BROKER=false): %s", exc)
            raise
    try:
        from trader.graph import get_compiled_graph

        await asyncio.to_thread(get_compiled_graph)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Graph init warning: %s", exc)
    review_task = asyncio.create_task(exit_review_loop(), name="exit-review-loop")
    fill_task = asyncio.create_task(fill_reconcile_loop(), name="fill-reconcile-loop")
    autopilot_task = asyncio.create_task(autopilot_loop(), name="autopilot-loop")
    background = (review_task, fill_task, autopilot_task)
    yield
    for task in background:
        task.cancel()
    # Gather together so nested awaitables settle before the loop closes;
    # cancelling one-by-one left orphaned tasks → "Task was destroyed…".
    await asyncio.gather(*background, return_exceptions=True)


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
    market: str = Field(default="us")


class ScanRequest(BaseModel):
    market: str = Field(default="us")


@app.get("/api/health")
async def health() -> dict[str, Any]:
    settings = get_settings()
    return {
        "ok": True,
        "trading_mode": settings.ibkr_trading_mode,
        "live_orders_enabled": settings.ibkr_live_orders_enabled,
        "mock_llm": settings.allow_mock_llm or not bool(settings.nous_api_key),
        "mock_broker": get_broker().using_mock,
        "autopilot_enabled": settings.auto_trade_enabled,
        "autopilot_armed": autopilot_armed(settings),
    }


@app.get("/api/models")
async def models() -> dict[str, str]:
    return get_settings().model_map()


@app.post("/api/runs")
async def create_run(
    body: StartRunRequest, session: AsyncSession = Depends(get_session)
) -> dict[str, Any]:
    try:
        market = parse_market(body.market)
        ticker = normalize_ticker(body.ticker, market)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    try:
        return await start_research_run(session, ticker, market=market)
    except Exception as exc:  # noqa: BLE001
        logger.exception("create_run failed")
        raise HTTPException(500, str(exc)) from exc


def _universe_names(market: str = "us") -> dict[str, str]:
    try:
        return {
            row["ticker"].upper(): row.get("name") or ""
            for row in load_universe(market=market)
        }
    except Exception:  # noqa: BLE001
        return {}


def _parse_market_param(market: str | None) -> str:
    try:
        return parse_market(market or "us")
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


@app.get("/api/runs")
async def runs(
    market: str = "us",
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    mid = _parse_market_param(market)
    rows = await store.list_runs(session, market=mid)
    names = _universe_names(mid)
    return [
        {
            "id": r.id,
            "ticker": r.ticker,
            "market": getattr(r, "market", None) or mid,
            "name": names.get(r.ticker.upper(), ""),
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
    mid = getattr(row, "market", None) or "us"
    names = _universe_names(mid)
    return {
        "id": row.id,
        "ticker": row.ticker,
        "market": mid,
        "name": names.get(row.ticker.upper(), ""),
        "status": row.status,
        "nodes": row.nodes,
        "state": row.state_snapshot,
        "error": row.error,
        "created_at": row.created_at,
        "updated_at": row.updated_at,
    }


@app.get("/api/proposals")
async def proposals(
    market: str = "us",
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    mid = _parse_market_param(market)
    rows = await store.list_open_proposals(session, market=mid)
    return [
        {
            "id": r.id,
            "run_id": r.run_id,
            "ticker": r.ticker,
            "market": getattr(r, "market", None) or mid,
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
        "market": getattr(row, "market", None) or "us",
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
async def book(market: str = "us") -> dict[str, Any]:
    mid = _parse_market_param(market)
    try:
        # Broker I/O is sync; keep it off the uvicorn event loop.
        snap = await asyncio.to_thread(get_book_snapshot, mid)
        data = snap.model_dump(mode="json")
        names = _universe_names(mid)
        for pos in data.get("positions") or []:
            pos["name"] = names.get(str(pos.get("ticker") or "").upper(), "")
        for order in data.get("open_orders") or []:
            order["name"] = names.get(str(order.get("ticker") or "").upper(), "")
        return data
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(503, f"book unavailable: {exc}") from exc


@app.post("/api/fills/reconcile")
async def fills_reconcile(session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    """Pull late IBKR fills for resting intents and open position plans."""
    try:
        return await reconcile_submitted_fills(session)
    except Exception as exc:  # noqa: BLE001
        logger.exception("fill reconcile failed")
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/fills")
async def fills(
    market: str = "us",
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    mid = _parse_market_param(market)
    rows = await store.list_fills(session, market=mid)
    names = _universe_names(mid)
    return [
        {
            "id": r.id,
            "intent_id": r.intent_id,
            "broker_order_id": r.broker_order_id,
            "ticker": r.ticker,
            "market": getattr(r, "market", None) or mid,
            "name": names.get(r.ticker.upper(), ""),
            "side": r.side,
            "quantity": r.quantity,
            "fill_price": r.fill_price,
            "account_id": r.account_id,
            "filled_at": r.filled_at,
        }
        for r in rows
    ]

def _candidate_out(row: Any) -> dict[str, Any]:
    if isinstance(row, dict):
        return {
            "ticker": row.get("ticker"),
            "rank": row.get("rank"),
            "score": row.get("score"),
            "sector": row.get("sector") or "",
            "name": row.get("name") or "",
            "reason": row.get("reason") or "",
            "signals": list(row.get("signals") or []),
            "headlines": list(row.get("headlines") or []),
        }
    return {
        "ticker": row.ticker,
        "rank": row.rank,
        "score": row.score,
        "sector": row.sector,
        "name": row.name,
        "reason": row.reason,
        "signals": list(row.signals or []),
        "headlines": list(row.headlines or []),
    }


async def _hold_tickers(session: AsyncSession, market: str = "us") -> set[str]:
    rows = await store.list_runs(session, limit=40, market=market)
    found: set[str] = set()
    for row in rows:
        thesis = (row.state_snapshot or {}).get("pm_thesis") or {}
        if thesis.get("side") == "HOLD":
            found.add(row.ticker.upper())
    return found


@app.post("/api/scans")
async def create_scan(
    body: ScanRequest | None = None,
    market: str = Query(default="us"),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    mid = _parse_market_param((body.market if body else None) or market)
    try:
        universe = load_universe(market=mid)
    except (FileNotFoundError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
    held: set[str] = set()
    try:
        book = await asyncio.to_thread(get_book_snapshot, mid)
        held = {p.ticker.upper() for p in book.positions if p.quantity > 0}
    except Exception as exc:  # noqa: BLE001
        logger.warning("scan continuing without book: %s", exc)
    holds = await _hold_tickers(session, mid)
    try:
        result = await asyncio.to_thread(
            run_scan,
            market=mid,
            universe=universe,
            held_tickers=held,
            hold_tickers=holds,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("scan failed")
        raise HTTPException(500, str(exc)) from exc
    scan = await store.save_scan(
        session,
        regime=result["regime"],
        universe_count=result["universe_count"],
        payload={"hot_sectors": result.get("hot_sectors") or [], "market": mid},
        candidates=result["candidates"],
        market=mid,
    )
    return {
        "id": scan.id,
        "market": mid,
        "regime": scan.regime,
        "universe_count": scan.universe_count,
        "hot_sectors": result.get("hot_sectors") or [],
        "created_at": scan.created_at,
        "candidates": [_candidate_out(row) for row in result["candidates"]],
    }


@app.get("/api/scans/latest")
async def latest_scan(
    market: str = "us",
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    mid = _parse_market_param(market)
    found = await store.latest_scan(session, market=mid)
    if not found:
        return {"id": None, "market": mid, "candidates": []}
    scan, rows = found
    return {
        "id": scan.id,
        "market": getattr(scan, "market", None) or mid,
        "regime": scan.regime,
        "universe_count": scan.universe_count,
        "hot_sectors": (scan.payload or {}).get("hot_sectors") or [],
        "created_at": scan.created_at,
        "candidates": [_candidate_out(row) for row in rows],
    }


@app.get("/api/position-plans")
async def position_plans(
    market: str = "us",
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    mid = _parse_market_param(market)
    plans = await store.list_open_plans(session, market=mid)
    planned = {row.ticker.upper() for row in plans}
    names = _universe_names(mid)
    unplanned: list[dict[str, Any]] = []
    book_error = None
    try:
        book = await asyncio.to_thread(get_book_snapshot, mid)
        unplanned = [
            {
                "ticker": pos.ticker,
                "quantity": pos.quantity,
                "avg_cost": pos.avg_cost,
                "name": names.get(pos.ticker.upper(), ""),
            }
            for pos in book.positions
            if pos.quantity > 0 and pos.ticker.upper() not in planned
        ]
    except Exception as exc:  # noqa: BLE001
        book_error = str(exc)
    plan_rows = []
    for row in plans:
        item = plan_to_dict(row)
        item["name"] = names.get(row.ticker.upper(), "")
        plan_rows.append(item)
    return {
        "plans": plan_rows,
        "unplanned": unplanned,
        "book_error": book_error,
        "market": mid,
    }


@app.post("/api/exits/review")
async def exits_review(
    market: str = "us",
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    mid = _parse_market_param(market)
    try:
        return await review_exits(session, scheduled=False, market=mid)
    except Exception as exc:  # noqa: BLE001
        logger.exception("exit review failed")
        raise HTTPException(500, str(exc)) from exc


@app.get("/api/audit")
async def audit(
    market: str | None = None,
    session: AsyncSession = Depends(get_session),
) -> list[dict[str, Any]]:
    mid = _parse_market_param(market) if market else None
    rows = await store.list_audit(session, market=mid)
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
