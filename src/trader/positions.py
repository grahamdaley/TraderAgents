"""Position plans opened by a buy fill and reduced by a sell fill."""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession

from trader import store
from trader.db import PositionPlanRow
from trader.schemas import utcnow

HORIZON_MIN = 14
HORIZON_MAX = 60


def clamp_horizon_days(days: int | None) -> int:
    try:
        value = int(days) if days is not None else 30
    except (TypeError, ValueError):
        value = 30
    return max(HORIZON_MIN, min(HORIZON_MAX, value))


def average_entry(
    old_qty: float, old_avg: float, add_qty: float, add_price: float
) -> tuple[float, float]:
    new_qty = old_qty + add_qty
    if new_qty <= 0:
        return old_qty, old_avg
    new_avg = (old_qty * old_avg + add_qty * add_price) / new_qty
    return new_qty, new_avg


async def record_fill_on_plan(session: AsyncSession, values: dict[str, Any]) -> None:
    fill = values.get("fill") or {}
    side = str(fill.get("side") or "")
    ticker = str(fill.get("ticker") or "").upper()
    try:
        qty = float(fill.get("quantity") or 0)
        price = float(fill.get("fill_price") or 0)
    except (TypeError, ValueError):
        return
    if not ticker or qty <= 0 or price <= 0:
        return

    existing = await store.get_open_plan(session, ticker)
    now = utcnow()
    if side == "BUY":
        thesis = values.get("pm_thesis") or {}
        indicators = (values.get("technicals_report") or {}).get("indicators") or {}
        horizon = clamp_horizon_days(thesis.get("horizon_days"))
        sma50 = indicators.get("sma50")
        summary = str(thesis.get("thesis") or "")
        run_id = str(values.get("run_id") or "")
        if existing:
            new_qty, new_avg = average_entry(existing.quantity, existing.avg_price, qty, price)
            existing.quantity = new_qty
            existing.avg_price = new_avg
            existing.horizon_days = horizon
            existing.review_after = now + timedelta(days=horizon)
            existing.thesis_summary = summary or existing.thesis_summary
            existing.run_id = run_id or existing.run_id
            if sma50 is not None:
                existing.entry_sma50 = float(sma50)
        else:
            session.add(
                PositionPlanRow(
                    id=str(uuid4()),
                    ticker=ticker,
                    run_id=run_id,
                    quantity=qty,
                    avg_price=price,
                    horizon_days=horizon,
                    review_after=now + timedelta(days=horizon),
                    entry_sma50=float(sma50) if sma50 is not None else None,
                    thesis_summary=summary,
                    status="open",
                    opened_at=now,
                )
            )
        await session.commit()
        return

    if side == "SELL" and existing:
        remaining = existing.quantity - qty
        if remaining <= 1e-6:
            existing.quantity = 0
            existing.status = "closed"
            existing.closed_at = now
        else:
            existing.quantity = remaining
        await session.commit()


async def postpone_plan(session: AsyncSession, ticker: str, horizon_days: int, summary: str) -> None:
    existing = await store.get_open_plan(session, ticker)
    if not existing:
        return
    horizon = clamp_horizon_days(horizon_days)
    existing.horizon_days = horizon
    existing.review_after = utcnow() + timedelta(days=horizon)
    if summary:
        existing.thesis_summary = summary
    await session.commit()


def plan_to_dict(row: PositionPlanRow) -> dict[str, Any]:
    return {
        "id": row.id,
        "ticker": row.ticker,
        "run_id": row.run_id,
        "quantity": row.quantity,
        "avg_price": row.avg_price,
        "horizon_days": row.horizon_days,
        "review_after": row.review_after,
        "entry_sma50": row.entry_sma50,
        "thesis_summary": row.thesis_summary,
        "status": row.status,
        "opened_at": row.opened_at,
        "closed_at": row.closed_at,
        "last_close": row.last_close,
        "last_sma50": row.last_sma50,
        "nearest_rule": row.nearest_rule,
        "last_checked_at": row.last_checked_at,
    }
