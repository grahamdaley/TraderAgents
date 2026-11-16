from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from trader.config import get_settings
from trader.db import (
    AuditRow,
    FillRow,
    IntentRow,
    PositionPlanRow,
    ProposalRow,
    RunRow,
    ScanCandidateRow,
    ScanRow,
)
from trader.schemas import (
    AuditEvent,
    ExecutionIntent,
    FillRecord,
    TradeProposal,
    utcnow,
)

logger = logging.getLogger(__name__)


async def create_run(
    session: AsyncSession,
    *,
    run_id: str,
    ticker: str,
    thread_id: str,
    nodes: dict[str, Any],
) -> RunRow:
    row = RunRow(
        id=run_id,
        ticker=ticker.upper(),
        status="running",
        thread_id=thread_id,
        nodes=nodes,
        state_snapshot={},
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def update_run(
    session: AsyncSession,
    run_id: str,
    *,
    status: str | None = None,
    nodes: dict[str, Any] | None = None,
    state_snapshot: dict[str, Any] | None = None,
    error: str | None = None,
) -> RunRow | None:
    row = await session.get(RunRow, run_id)
    if not row:
        return None
    if status is not None:
        row.status = status
    if nodes is not None:
        row.nodes = nodes
    if state_snapshot is not None:
        row.state_snapshot = state_snapshot
    if error is not None:
        row.error = error
    row.updated_at = utcnow()
    await session.commit()
    await session.refresh(row)
    return row


async def get_run(session: AsyncSession, run_id: str) -> RunRow | None:
    return await session.get(RunRow, run_id)


async def claim_run_for_decision(session: AsyncSession, run_id: str) -> RunRow | None:
    """Atomically claim an awaiting_human run so duplicate decisions cannot race."""
    result = await session.execute(
        select(RunRow).where(RunRow.id == run_id).with_for_update()
    )
    row = result.scalar_one_or_none()
    if not row or row.status != "awaiting_human":
        return None
    row.status = "processing_decision"
    row.updated_at = utcnow()
    await session.commit()
    await session.refresh(row)
    return row


def list_open_proposal_tickers_sync() -> set[str]:
    """Sync lookup for policy_node (runs inside LangGraph worker threads)."""
    settings = get_settings()
    engine = create_engine(settings.database_url_sync)
    try:
        with engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT DISTINCT ticker FROM proposals "
                    "WHERE status = 'awaiting_human'"
                )
            )
            return {str(r[0]).upper() for r in rows if r[0]}
    except Exception:  # noqa: BLE001
        return set()
    finally:
        engine.dispose()


async def list_runs(session: AsyncSession, limit: int = 50) -> list[RunRow]:
    result = await session.execute(
        select(RunRow).order_by(RunRow.created_at.desc()).limit(limit)
    )
    return list(result.scalars())


async def upsert_proposal(session: AsyncSession, proposal: TradeProposal) -> ProposalRow:
    existing = await session.get(ProposalRow, proposal.proposal_id)
    payload = proposal.model_dump(mode="json")
    if existing:
        existing.status = proposal.status.value
        existing.content_hash = proposal.content_hash
        existing.payload = payload
        existing.expires_at = proposal.expires_at
        row = existing
    else:
        row = ProposalRow(
            id=proposal.proposal_id,
            run_id=proposal.run_id,
            ticker=proposal.ticker,
            status=proposal.status.value,
            content_hash=proposal.content_hash,
            payload=payload,
            expires_at=proposal.expires_at,
        )
        session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def list_open_proposals(session: AsyncSession) -> list[ProposalRow]:
    result = await session.execute(
        select(ProposalRow)
        .where(ProposalRow.status == "awaiting_human")
        .order_by(ProposalRow.created_at.desc())
    )
    return list(result.scalars())


async def get_proposal(session: AsyncSession, proposal_id: str) -> ProposalRow | None:
    return await session.get(ProposalRow, proposal_id)


async def save_intent(session: AsyncSession, intent: ExecutionIntent) -> IntentRow:
    row = IntentRow(
        id=intent.intent_id,
        proposal_id=intent.proposal_id,
        run_id=intent.run_id,
        approval_id=intent.approval_id,
        client_order_id=intent.client_order_id,
        payload=intent.model_dump(mode="json"),
        status="created",
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def mark_intent_submitted(
    session: AsyncSession, intent_id: str, broker_order_id: str
) -> None:
    row = await session.get(IntentRow, intent_id)
    if row:
        if broker_order_id:
            row.broker_order_id = broker_order_id
        row.status = "submitted"
        await session.commit()


async def mark_intent_status(
    session: AsyncSession,
    intent_id: str,
    status: str,
    *,
    broker_order_id: str | None = None,
) -> None:
    row = await session.get(IntentRow, intent_id)
    if not row:
        return
    row.status = status
    if broker_order_id:
        row.broker_order_id = broker_order_id
    await session.commit()


async def get_fill_for_intent(session: AsyncSession, intent_id: str) -> FillRow | None:
    result = await session.execute(
        select(FillRow).where(FillRow.intent_id == intent_id).limit(1)
    )
    return result.scalar_one_or_none()


async def list_submitted_intents_without_fills(
    session: AsyncSession, *, limit: int = 200
) -> list[IntentRow]:
    """Resting intents that still need fill reconciliation.

    Includes legacy ``created`` rows from before submit_result was persisted
    on graph state (those never flipped to ``submitted``).
    """
    result = await session.execute(
        select(IntentRow)
        .outerjoin(FillRow, FillRow.intent_id == IntentRow.id)
        .where(IntentRow.status.in_(("submitted", "created")))
        .where(FillRow.id.is_(None))
        .order_by(IntentRow.created_at.asc())
        .limit(limit)
    )
    return list(result.scalars())


async def save_fill(session: AsyncSession, fill: FillRecord) -> FillRow:
    row = FillRow(
        id=fill.fill_id,
        intent_id=fill.intent_id,
        broker_order_id=fill.broker_order_id,
        ticker=fill.ticker,
        side=fill.side.value,
        quantity=fill.quantity,
        fill_price=fill.fill_price,
        account_id=fill.account_id,
        payload=fill.model_dump(mode="json"),
        filled_at=fill.filled_at,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def save_intent_and_fill(
    session: AsyncSession,
    *,
    intent: ExecutionIntent,
    fill: FillRecord,
) -> tuple[IntentRow, FillRow]:
    """Persist intent + fill in one commit so they cannot diverge."""
    intent_row = IntentRow(
        id=intent.intent_id,
        proposal_id=intent.proposal_id,
        run_id=intent.run_id,
        approval_id=intent.approval_id,
        client_order_id=intent.client_order_id,
        broker_order_id=fill.broker_order_id,
        payload=intent.model_dump(mode="json"),
        status="submitted",
    )
    fill_row = FillRow(
        id=fill.fill_id,
        intent_id=fill.intent_id,
        broker_order_id=fill.broker_order_id,
        ticker=fill.ticker,
        side=fill.side.value,
        quantity=fill.quantity,
        fill_price=fill.fill_price,
        account_id=fill.account_id,
        payload=fill.model_dump(mode="json"),
        filled_at=fill.filled_at,
    )
    session.add(intent_row)
    session.add(fill_row)
    await session.commit()
    await session.refresh(intent_row)
    await session.refresh(fill_row)
    return intent_row, fill_row


async def list_fills(session: AsyncSession, limit: int = 100) -> list[FillRow]:
    result = await session.execute(
        select(FillRow).order_by(FillRow.filled_at.desc()).limit(limit)
    )
    return list(result.scalars())


async def add_audit(
    session: AsyncSession,
    *,
    event_type: str,
    actor: str = "system",
    run_id: str | None = None,
    payload: dict[str, Any] | None = None,
) -> AuditRow:
    event = AuditEvent(
        event_id=str(uuid4()),
        run_id=run_id,
        event_type=event_type,
        actor=actor,
        payload=payload or {},
    )
    row = AuditRow(
        id=event.event_id,
        run_id=run_id,
        event_type=event_type,
        actor=actor,
        payload=event.payload,
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    return row


async def save_scan(
    session: AsyncSession,
    *,
    regime: str,
    universe_count: int,
    payload: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> ScanRow:
    scan_id = str(uuid4())
    scan = ScanRow(
        id=scan_id,
        regime=regime,
        universe_count=universe_count,
        payload=payload,
    )
    session.add(scan)
    for candidate in candidates:
        session.add(
            ScanCandidateRow(
                id=str(uuid4()),
                scan_id=scan_id,
                ticker=candidate["ticker"],
                rank=int(candidate["rank"]),
                score=float(candidate["score"]),
                sector=candidate.get("sector") or "",
                name=candidate.get("name") or "",
                reason=candidate.get("reason") or "",
                signals=list(candidate.get("signals") or []),
                headlines=list(candidate.get("headlines") or []),
            )
        )
    await session.commit()
    await session.refresh(scan)
    return scan


async def latest_scan(
    session: AsyncSession,
) -> tuple[ScanRow, list[ScanCandidateRow]] | None:
    result = await session.execute(
        select(ScanRow).order_by(ScanRow.created_at.desc()).limit(1)
    )
    scan = result.scalar_one_or_none()
    if not scan:
        return None
    rows = await session.execute(
        select(ScanCandidateRow)
        .where(ScanCandidateRow.scan_id == scan.id)
        .order_by(ScanCandidateRow.rank)
    )
    return scan, list(rows.scalars())


async def get_open_plan(session: AsyncSession, ticker: str) -> PositionPlanRow | None:
    result = await session.execute(
        select(PositionPlanRow)
        .where(
            PositionPlanRow.ticker == ticker.upper(),
            PositionPlanRow.status == "open",
        )
        .order_by(PositionPlanRow.opened_at.desc())
    )
    rows = list(result.scalars())
    if not rows:
        return None
    if len(rows) > 1:
        logger.warning(
            "multiple open plans for %s; keeping newest and closing %s older",
            ticker.upper(),
            len(rows) - 1,
        )
        for older in rows[1:]:
            older.status = "closed"
            older.closed_at = utcnow()
    return rows[0]


async def list_open_plans(session: AsyncSession) -> list[PositionPlanRow]:
    result = await session.execute(
        select(PositionPlanRow)
        .where(PositionPlanRow.status == "open")
        .order_by(PositionPlanRow.opened_at)
    )
    return list(result.scalars())


async def exit_review_done(session: AsyncSession, day: str) -> bool:
    result = await session.execute(
        select(AuditRow)
        .where(AuditRow.event_type == "exit_review_completed")
        .order_by(AuditRow.created_at.desc())
        .limit(30)
    )
    for row in result.scalars():
        if (row.payload or {}).get("date") == day:
            return True
    return False


async def list_audit(session: AsyncSession, limit: int = 200) -> list[AuditRow]:
    result = await session.execute(
        select(AuditRow).order_by(AuditRow.created_at.desc()).limit(limit)
    )
    return list(result.scalars())
