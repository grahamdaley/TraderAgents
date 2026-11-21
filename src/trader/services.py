from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import uuid4

from langgraph.types import Command
from sqlalchemy.ext.asyncio import AsyncSession

from trader import store
from trader.broker import get_broker
from trader.graph import get_compiled_graph, initial_state, invoke_graph_locked
from trader.positions import postpone_plan, record_fill_on_plan
from trader.schemas import (
    ApprovalRequest,
    ExecutionIntent,
    FillRecord,
    RunStatus,
    TradeProposal,
)

logger = logging.getLogger(__name__)

AUTOPILOT_OPERATOR = "autopilot"


def decision_event_type(operator: str) -> str:
    return "auto_decision" if operator == AUTOPILOT_OPERATOR else "human_decision"


def _serialize_state(values: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in (values or {}).items():
        if hasattr(value, "model_dump"):
            out[key] = value.model_dump(mode="json")
        else:
            out[key] = value
    return out


async def start_research_run(
    session: AsyncSession,
    ticker: str,
    *,
    market: str = "us",
    run_kind: str = "research",
    position_context: dict[str, Any] | None = None,
    exit_reason: str | None = None,
) -> dict[str, Any]:
    run_id = str(uuid4())
    thread_id = run_id
    state = initial_state(
        run_id,
        ticker,
        market=market,
        run_kind=run_kind,
        position_context=position_context,
        exit_reason=exit_reason,
    )
    await store.create_run(
        session,
        run_id=run_id,
        ticker=ticker,
        thread_id=thread_id,
        nodes=state["nodes"],
        market=market,
    )
    await store.add_audit(
        session,
        event_type="run_started",
        run_id=run_id,
        payload={"ticker": ticker.upper(), "run_kind": run_kind, "market": market},
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
    values.setdefault("run_id", run_id)
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

    if run_kind == "horizon_review":
        thesis = values.get("pm_thesis") or {}
        if thesis.get("side") == "HOLD":
            await postpone_plan(
                session,
                ticker.upper(),
                int(thesis.get("horizon_days") or 30),
                str(thesis.get("thesis") or ""),
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
    values.setdefault("run_id", run_id)
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
    submit_result = values.get("submit_result") or {}
    if intent_data and fill:
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
        await record_fill_on_plan(session, values)
    elif intent_data and status == RunStatus.SUBMITTED.value:
        intent = ExecutionIntent.model_validate(intent_data)
        intent.approval_id = approval_id
        # Prefer submit_result; fall back to empty — reconcile matches on client_order_id.
        broker_order_id = str(submit_result.get("broker_order_id") or "")
        if broker_order_id in {"", "0"}:
            broker_order_id = ""
        await store.save_intent(session, intent)
        await store.mark_intent_submitted(
            session, intent.intent_id, broker_order_id or ""
        )
        await store.add_audit(
            session,
            event_type="order_submitted",
            actor=request.operator,
            run_id=run_id,
            payload={
                "broker_order_id": broker_order_id,
                "client_order_id": intent.client_order_id,
                "broker_status": submit_result.get("broker_status"),
                "ticker": intent.ticker,
                "quantity": intent.quantity,
                "limit_price": intent.limit_price,
                "message": submit_result.get("message"),
            },
        )

    # Audit the decision only after resume completed successfully.
    side = None
    if isinstance(proposal_data, dict):
        side = proposal_data.get("side")
    await store.add_audit(
        session,
        event_type=decision_event_type(request.operator),
        actor=request.operator,
        run_id=run_id,
        payload={**decision, "final_status": status, "side": side},
    )

    return {"run_id": run_id, "status": status, "state": values}


async def maybe_auto_approve(
    session: AsyncSession,
    *,
    run_id: str,
    state: dict[str, Any],
    buys_used: int,
    settings: Any | None = None,
) -> dict[str, Any]:
    """Resume an awaiting run when paper autopilot is armed and the proposal clears the floor.

    Exit sells do not consume the daily buy cap. A skip leaves the proposal in the Inbox.
    """
    from trader.autopilot import approval_skip_reason
    from trader.config import get_settings

    settings = settings or get_settings()
    reason = approval_skip_reason(state=state, buys_used=buys_used, settings=settings)
    if reason:
        return {"approved": False, "reason": reason, "buys_used": buys_used}

    proposal = (state or {}).get("trade_proposal") or {}
    side = str(proposal.get("side") or "").upper()
    request = ApprovalRequest(
        action="approve",
        operator=AUTOPILOT_OPERATOR,
        note="paper autopilot",
    )
    result = await resume_with_decision(session, run_id=run_id, request=request)
    new_buys = buys_used + (1 if side == "BUY" else 0)
    return {
        "approved": True,
        "reason": "approved",
        "buys_used": new_buys,
        "status": result.get("status"),
    }


def get_book_snapshot(market: str = "us"):
    return get_broker().get_book(market)
