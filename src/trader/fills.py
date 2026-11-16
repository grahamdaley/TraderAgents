"""Reconcile resting IBKR orders into fills + position plans."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from trader import store
from trader.broker import get_broker
from trader.db import IntentRow, get_session_factory
from trader.positions import record_fill_on_plan
from trader.schemas import FillRecord, RunStatus, Side, utcnow

logger = logging.getLogger(__name__)

_reconcile_lock = asyncio.Lock()
TERMINAL_CANCEL = {"Cancelled", "ApiCancelled", "Inactive"}
TERMINAL_FILL = {"Filled"}


def classify_order_outcome(
    *,
    broker_status: str,
    filled_qty: float,
    avg_price: float,
    remaining: float | None = None,
) -> str | None:
    """Return ``filled``, ``cancelled``, or None if still working."""
    status = (broker_status or "").strip()
    qty = float(filled_qty or 0)
    price = float(avg_price or 0)
    if qty > 0 and price > 0 and (
        status in TERMINAL_FILL
        or status in TERMINAL_CANCEL
        or (remaining is not None and float(remaining) <= 1e-9)
    ):
        return "filled"
    if status in TERMINAL_CANCEL and qty <= 1e-9:
        return "cancelled"
    return None


async def _apply_fill(
    session: AsyncSession,
    intent: IntentRow,
    *,
    outcome: dict[str, Any],
) -> dict[str, Any]:
    payload = intent.payload or {}
    side_raw = str(payload.get("side") or outcome.get("side") or "BUY").upper()
    try:
        side = Side(side_raw)
    except ValueError:
        side = Side.BUY
    ticker = str(
        payload.get("ticker") or outcome.get("ticker") or ""
    ).upper()
    filled_qty = float(outcome.get("filled_qty") or 0)
    avg_price = float(outcome.get("avg_price") or 0)
    broker_order_id = str(
        outcome.get("broker_order_id") or intent.broker_order_id or ""
    )
    if not ticker or filled_qty <= 0 or avg_price <= 0:
        return {"intent_id": intent.id, "action": "skipped_incomplete_fill"}

    account_id = await _account_id()
    fill = FillRecord(
        intent_id=intent.id,
        broker_order_id=broker_order_id,
        ticker=ticker,
        side=side,
        quantity=filled_qty,
        fill_price=avg_price,
        account_id=account_id,
        filled_at=utcnow(),
    )
    await store.save_fill(session, fill)
    await store.mark_intent_status(
        session, intent.id, "filled", broker_order_id=broker_order_id
    )

    run = await store.get_run(session, intent.run_id)
    snapshot = dict(run.state_snapshot or {}) if run else {}
    snapshot["fill"] = fill.model_dump(mode="json")
    snapshot["status"] = RunStatus.FILLED.value
    snapshot.setdefault("execution_intent", payload)
    await store.update_run(
        session,
        intent.run_id,
        status=RunStatus.FILLED.value,
        state_snapshot=snapshot,
        error=None,
    )
    if intent.proposal_id:
        proposal = await store.get_proposal(session, intent.proposal_id)
        if proposal:
            proposal.status = RunStatus.FILLED.value
            await session.commit()

    await record_fill_on_plan(session, snapshot)
    await store.add_audit(
        session,
        event_type="order_filled",
        actor="system",
        run_id=intent.run_id,
        payload={
            "broker_order_id": fill.broker_order_id,
            "ticker": fill.ticker,
            "quantity": fill.quantity,
            "fill_price": fill.fill_price,
            "account_id": fill.account_id,
            "source": "reconcile",
        },
    )
    return {
        "intent_id": intent.id,
        "action": "filled",
        "ticker": ticker,
        "quantity": filled_qty,
        "fill_price": avg_price,
    }


async def _account_id() -> str:
    try:
        book = await asyncio.to_thread(get_broker().get_book)
        return book.account_id or ""
    except Exception:  # noqa: BLE001
        return ""


async def _apply_cancel(
    session: AsyncSession, intent: IntentRow, *, outcome: dict[str, Any]
) -> dict[str, Any]:
    broker_order_id = str(
        outcome.get("broker_order_id") or intent.broker_order_id or ""
    )
    await store.mark_intent_status(
        session, intent.id, "cancelled", broker_order_id=broker_order_id or None
    )
    run = await store.get_run(session, intent.run_id)
    if run and run.status == RunStatus.SUBMITTED.value:
        snapshot = dict(run.state_snapshot or {})
        snapshot["status"] = RunStatus.CANCELLED.value
        await store.update_run(
            session,
            intent.run_id,
            status=RunStatus.CANCELLED.value,
            state_snapshot=snapshot,
            error=f"IBKR order cancelled ({outcome.get('status')})",
        )
    await store.add_audit(
        session,
        event_type="order_cancelled",
        actor="system",
        run_id=intent.run_id,
        payload={
            "broker_order_id": broker_order_id,
            "ticker": (intent.payload or {}).get("ticker"),
            "broker_status": outcome.get("status"),
            "source": "reconcile",
        },
    )
    return {
        "intent_id": intent.id,
        "action": "cancelled",
        "ticker": (intent.payload or {}).get("ticker"),
    }


async def reconcile_submitted_fills(session: AsyncSession) -> dict[str, Any]:
    """Poll IBKR for submitted intents and record late fills/cancels."""
    async with _reconcile_lock:
        intents = await store.list_submitted_intents_without_fills(session)
        if not intents:
            return {"checked": 0, "actions": []}

        queries = [
            {
                "client_order_id": str(
                    (row.payload or {}).get("client_order_id") or row.client_order_id or ""
                ),
                "broker_order_id": str(row.broker_order_id or ""),
            }
            for row in intents
        ]
        try:
            outcomes = await asyncio.to_thread(
                get_broker().lookup_order_outcomes, queries
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("fill reconcile broker lookup failed")
            return {"checked": len(intents), "actions": [], "error": str(exc)}

        actions: list[dict[str, Any]] = []
        for intent in intents:
            client_id = str(
                (intent.payload or {}).get("client_order_id")
                or intent.client_order_id
                or ""
            )
            broker_id = str(intent.broker_order_id or "")
            outcome = outcomes.get(client_id) or outcomes.get(broker_id)
            if not outcome:
                actions.append(
                    {
                        "intent_id": intent.id,
                        "action": "pending",
                        "ticker": (intent.payload or {}).get("ticker"),
                    }
                )
                continue

            existing = await store.get_fill_for_intent(session, intent.id)
            if existing:
                await store.mark_intent_status(session, intent.id, "filled")
                actions.append({"intent_id": intent.id, "action": "already_filled"})
                continue

            kind = classify_order_outcome(
                broker_status=str(outcome.get("status") or ""),
                filled_qty=float(outcome.get("filled_qty") or 0),
                avg_price=float(outcome.get("avg_price") or 0),
                remaining=outcome.get("remaining"),
            )
            if kind == "filled":
                actions.append(await _apply_fill(session, intent, outcome=outcome))
            elif kind == "cancelled":
                actions.append(await _apply_cancel(session, intent, outcome=outcome))
            else:
                actions.append(
                    {
                        "intent_id": intent.id,
                        "action": "working",
                        "broker_status": outcome.get("status"),
                        "ticker": (intent.payload or {}).get("ticker"),
                    }
                )
        return {"checked": len(intents), "actions": actions}


async def fill_reconcile_loop(interval_seconds: float = 30.0) -> None:
    """Background poll so resting DAY/GTC fills open position plans."""
    while True:
        try:
            factory = get_session_factory()
            async with factory() as session:
                result = await reconcile_submitted_fills(session)
            filled = [
                a for a in result.get("actions") or [] if a.get("action") == "filled"
            ]
            if filled:
                logger.info(
                    "Fill reconcile recorded %s fill(s): %s",
                    len(filled),
                    ", ".join(
                        f"{a.get('ticker')}@{a.get('fill_price')}" for a in filled
                    ),
                )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("fill reconcile loop failed")
        await asyncio.sleep(interval_seconds)
