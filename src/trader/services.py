from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from trader.broker import get_broker
from trader.graph import get_compiled_graph, initial_state, invoke_graph_locked
from trader.schemas import (
    ApprovalRequest,
    ExecutionIntent,
    FillRecord,
    RunStatus,
    TradeProposal,
)
from trader import store

logger = logging.getLogger(__name__)


def _serialize_state(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (values or {}).items():
        if hasattr(value, "model_dump"):
            out[key] = value.model_dump(mode="json")
        else:
            out[key] = value
    return out


async def start_research_run(session: AsyncSession, ticker: str) -> dict[str, Any]:
    run_id = str(uuid4())
    thread_id = run_id
    state = initial_state(run_id, ticker)
    await store.create_run(
        session,
        run_id=run_id,
        ticker=ticker,
        thread_id=thread_id,
        nodes=state["nodes"],
    )
    await store.add_audit(
        session,
        event_type="run_started",
        run_id=run_id,
        payload={"ticker": ticker.upper()},
    )

    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": thread_id}}
    try:
        result, snapshot, interrupt_exc = await asyncio.to_thread(
            invoke_graph_locked, graph, state, config
        )
        if interrupt_exc:
            logger.info("run interrupted for human approval: %s", run_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("run failed")
        await store.update_run(
            session,
            run_id,
            status=RunStatus.FAILED.value,
            error=str(exc),
        )
        await store.add_audit(
            session,
            event_type="run_failed",
            run_id=run_id,
            payload={"error": str(exc)},
        )
        raise

    values = _serialize_state(snapshot.values if snapshot else result)
    interrupted = bool(snapshot and snapshot.next) or (
        bool(values.get("trade_proposal"))
        and values.get("status") == RunStatus.AWAITING_HUMAN.value
    )

    status = values.get("status") or (
        RunStatus.AWAITING_HUMAN.value if interrupted else RunStatus.REJECTED.value
    )
    if interrupted:
        status = RunStatus.AWAITING_HUMAN.value

    await store.update_run(
        session,
        run_id,
        status=status,
        nodes=values.get("nodes") or state["nodes"],
        state_snapshot=values,
        error=values.get("error") or values.get("policy_rejection"),
    )

    proposal_data = values.get("trade_proposal")
    if proposal_data:
        proposal = TradeProposal.model_validate(proposal_data)
        if interrupted:
            proposal.status = RunStatus.AWAITING_HUMAN
        await store.upsert_proposal(session, proposal)
        await store.add_audit(
            session,
            event_type="proposal_created",
            run_id=run_id,
            payload={
                "proposal_id": proposal.proposal_id,
                "ticker": proposal.ticker,
                "side": proposal.side.value,
                "quantity": proposal.quantity,
                "content_hash": proposal.content_hash,
            },
        )

    return {
        "run_id": run_id,
        "thread_id": thread_id,
        "status": status,
        "interrupted": interrupted,
        "state": values,
    }


async def resume_with_decision(
    session: AsyncSession,
    *,
    run_id: str,
    request: ApprovalRequest,
) -> dict[str, Any]:
    row = await store.claim_run_for_decision(session, run_id)
    if not row:
        existing = await store.get_run(session, run_id)
        if not existing:
            raise KeyError(f"run {run_id} not found")
        raise ValueError(
            f"run {run_id} is '{existing.status}', expected 'awaiting_human'"
        )

    approval_id = str(uuid4())
    decision = {
        "action": request.action,
        "operator": request.operator,
        "quantity": request.quantity,
        "limit_price": request.limit_price,
        "note": request.note,
        "approval_id": approval_id,
    }

    graph = get_compiled_graph()
    config = {"configurable": {"thread_id": row.thread_id}}
    try:
        result, snapshot, _ = await asyncio.to_thread(
            invoke_graph_locked, graph, Command(resume=decision), config
        )
    except Exception as exc:  # noqa: BLE001
        await store.update_run(
            session,
            run_id,
            status=RunStatus.FAILED.value,
            error=str(exc),
        )
        await store.add_audit(
            session,
            event_type="decision_failed",
            actor=request.operator,
            run_id=run_id,
            payload={**decision, "error": str(exc)},
        )
        raise

    values = _serialize_state(snapshot.values if snapshot else result)
    status = values.get("status") or RunStatus.REJECTED.value
    await store.update_run(
        session,
        run_id,
        status=status,
        nodes=values.get("nodes") or row.nodes,
        state_snapshot=values,
        error=values.get("error"),
    )

    proposal_data = values.get("trade_proposal")
    if proposal_data:
        proposal = TradeProposal.model_validate(proposal_data)
        if status in RunStatus._value2member_map_:
            proposal.status = RunStatus(status)
        await store.upsert_proposal(session, proposal)

    fill = values.get("fill")
    intent_data = values.get("execution_intent")
    if fill and intent_data:
        intent = ExecutionIntent.model_validate(intent_data)
        intent.approval_id = approval_id
        fill_rec = FillRecord.model_validate(fill)
        await store.save_intent_and_fill(session, intent=intent, fill=fill_rec)
        await store.add_audit(
            session,
            event_type="order_filled",
            actor=request.operator,
            run_id=run_id,
            payload={
                "broker_order_id": fill_rec.broker_order_id,
                "ticker": fill_rec.ticker,
                "quantity": fill_rec.quantity,
                "fill_price": fill_rec.fill_price,
                "account_id": fill_rec.account_id,
            },
        )

    # Audit the human decision only after resume completed successfully.
    await store.add_audit(
        session,
        event_type="human_decision",
        actor=request.operator,
        run_id=run_id,
        payload={**decision, "final_status": status},
    )

    return {"run_id": run_id, "status": status, "state": values}


def get_book_snapshot():
    return get_broker().get_book()
