"""Daily exit review. Mechanical breaks propose a flatten; an elapsed horizon re-runs the committee."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from trader import store
from trader.config import get_settings
from trader.db import get_session_factory
from trader.schemas import utcnow
from trader.services import get_book_snapshot, start_research_run
from trader.tools.market_data import get_market_data_tools

logger = logging.getLogger(__name__)

_review_lock = asyncio.Lock()


def classify_exit(
    *,
    last_close: float | None,
    avg_price: float,
    sma50: float | None,
    review_after: datetime,
    now: datetime,
    max_loss_pct: float,
) -> tuple[str | None, str]:
    """Return (trigger, nearest rule).

    A loss break wins over a trend break, and either wins over an elapsed horizon.
    """
    loss = False
    trend = False
    if last_close is not None and avg_price > 0:
        loss = last_close <= avg_price * (1 - max_loss_pct)
    if last_close is not None and sma50 is not None and sma50 > 0:
        trend = last_close < sma50
    review_at = review_after
    if review_at.tzinfo is None:
        review_at = review_at.replace(tzinfo=UTC)
    horizon = now >= review_at

    if loss:
        trigger: str | None = "loss"
    elif trend:
        trigger = "trend"
    elif horizon:
        trigger = "horizon"
    else:
        trigger = None

    slacks: list[tuple[str, float]] = []
    if last_close is not None and avg_price > 0:
        slacks.append(("loss", (last_close / avg_price - 1) + max_loss_pct))
    if last_close is not None and sma50 is not None and sma50 > 0:
        slacks.append(("trend", last_close / sma50 - 1))
    slacks.append(("horizon", (review_at - now).total_seconds() / 86400 / 30))
    nearest = trigger or min(slacks, key=lambda item: item[1])[0]
    return trigger, nearest


def review_window_open(now: datetime, hour_utc: int) -> bool:
    if now.weekday() >= 5:
        return False
    slot = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    return now >= slot


def next_review_at(now: datetime, hour_utc: int) -> datetime:
    """Next weekday at hour_utc strictly after `now` if today's slot has passed."""
    slot = now.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    if now < slot and slot.weekday() < 5:
        return slot
    slot = slot + timedelta(days=1)
    slot = slot.replace(hour=hour_utc, minute=0, second=0, microsecond=0)
    while slot.weekday() >= 5:
        slot = slot + timedelta(days=1)
    return slot


def _position_qty(book, ticker: str) -> float:
    for pos in book.positions:
        if pos.ticker.upper() == ticker.upper():
            return float(pos.quantity)
    return 0.0


def working_sell_tickers(book) -> set[str]:
    """Tickers with a resting IBKR SELL (submitted but not yet filled)."""
    out: set[str] = set()
    for order in book.open_orders or []:
        side = str(order.get("side") or "").upper()
        if side != "SELL":
            continue
        ticker = str(order.get("ticker") or "").upper()
        if ticker:
            out.add(ticker)
    return out


def _exit_reason(trigger: str, last_close: float | None, avg_price: float, sma50: float | None) -> str:
    if trigger == "loss":
        return (
            f"Exit rule: last close {last_close:.2f} is at least the max-loss "
            f"distance below average price {avg_price:.2f}. Flatten the long."
        )
    return (
        f"Exit rule: last close {last_close:.2f} is below SMA50 "
        f"{sma50 if sma50 is not None else float('nan'):.2f}. Flatten the long."
    )


async def review_exits(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    scheduled: bool = False,
    include_horizon: bool = True,
) -> dict[str, Any]:
    """Evaluate open plans.

    The scheduled pass writes an audit row and will not run twice on the same UTC date.
    A manual pass still runs. Open Inbox proposals, in-flight runs, and resting /
    submitted SELLs (IBKR open orders or desk intents awaiting fill) block duplicates.
    Set include_horizon False to record an elapsed horizon without starting research.
    """
    async with _review_lock:
        moment = now or utcnow()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        day = moment.date().isoformat()
        if scheduled and await store.exit_review_done(session, day):
            return {"skipped": True, "date": day, "actions": []}

        settings = get_settings()
        plans = await store.list_open_plans(session)
        open_rows = await store.list_open_proposals(session)
        blocked = {row.ticker.upper() for row in open_rows}
        runs = await store.list_runs(session, limit=100)
        for run in runs:
            if run.status in {"running", "awaiting_human", "processing_decision"}:
                blocked.add(run.ticker.upper())

        # Desk intents already sent to IBKR as SELL, waiting for fill.
        pending_sells: set[str] = set()
        for intent in await store.list_submitted_intents_without_fills(session):
            payload = intent.payload or {}
            if str(payload.get("side") or "").upper() != "SELL":
                continue
            ticker = str(payload.get("ticker") or "").upper()
            if ticker:
                pending_sells.add(ticker)

        try:
            book = get_book_snapshot()
        except Exception as exc:
            logger.exception("exit review skipped; book unavailable")
            return {"skipped": True, "date": day, "actions": [], "error": str(exc)}

        pending_sells |= working_sell_tickers(book)

        tools = get_market_data_tools()
        actions: list[dict[str, Any]] = []
        horizon_started = 0
        for plan in plans:
            ticker = plan.ticker.upper()
            book_qty = _position_qty(book, ticker)
            if book_qty <= 0:
                plan.status = "closed"
                plan.closed_at = moment
                plan.quantity = 0
                actions.append({"ticker": ticker, "action": "closed_flat"})
                continue
            if ticker in blocked:
                actions.append({"ticker": ticker, "action": "skipped_busy"})
                continue
            if ticker in pending_sells:
                actions.append({"ticker": ticker, "action": "skipped_working_sell"})
                continue

            indicators: dict[str, Any] = {}
            try:
                indicators = tools.compute_indicators(ticker).get("indicators") or {}
            except Exception:
                logger.exception("exit indicators failed for %s", ticker)
                plan.nearest_rule = "data_error"
                plan.last_checked_at = moment
                reason = (
                    f"Exit rule: market data unavailable for {ticker}. "
                    "Flatten the long."
                )
                try:
                    started = await start_research_run(
                        session,
                        ticker,
                        run_kind="exit_rule",
                        exit_reason=reason,
                    )
                    actions.append(
                        {
                            "ticker": ticker,
                            "action": "exit_proposal",
                            "trigger": "data_error",
                            "run_id": started.get("run_id"),
                            "status": started.get("status"),
                        }
                    )
                    blocked.add(ticker)
                except Exception as exc:
                    logger.exception("exit proposal failed for %s", ticker)
                    actions.append(
                        {"ticker": ticker, "action": "error", "error": str(exc)}
                    )
                continue
            last_close = indicators.get("last_close")
            sma50 = indicators.get("sma50")
            trigger, nearest = classify_exit(
                last_close=last_close,
                avg_price=plan.avg_price,
                sma50=sma50,
                review_after=plan.review_after,
                now=moment,
                max_loss_pct=settings.exit_max_loss_pct,
            )
            plan.last_close = last_close
            plan.last_sma50 = sma50
            plan.nearest_rule = nearest
            plan.last_checked_at = moment

            if trigger in {"loss", "trend"}:
                reason = _exit_reason(trigger, last_close, plan.avg_price, sma50)
                try:
                    started = await start_research_run(
                        session,
                        ticker,
                        run_kind="exit_rule",
                        exit_reason=reason,
                    )
                    actions.append(
                        {
                            "ticker": ticker,
                            "action": "exit_proposal",
                            "trigger": trigger,
                            "run_id": started.get("run_id"),
                            "status": started.get("status"),
                        }
                    )
                    blocked.add(ticker)
                except Exception as exc:
                    logger.exception("exit proposal failed for %s", ticker)
                    actions.append({"ticker": ticker, "action": "error", "error": str(exc)})
                continue

            if trigger == "horizon" and not include_horizon:
                actions.append({"ticker": ticker, "action": "horizon_skipped"})
                continue

            if trigger == "horizon" and horizon_started < settings.horizon_review_cap:
                days_held = max(0, (moment - plan.opened_at).days)
                context = {
                    "quantity": plan.quantity,
                    "avg_price": plan.avg_price,
                    "days_held": days_held,
                    "horizon_days": plan.horizon_days,
                    "thesis_summary": plan.thesis_summary,
                    "review_reason": "horizon_elapsed",
                }
                try:
                    started = await start_research_run(
                        session,
                        ticker,
                        run_kind="horizon_review",
                        position_context=context,
                    )
                    horizon_started += 1
                    actions.append(
                        {
                            "ticker": ticker,
                            "action": "horizon_review",
                            "run_id": started.get("run_id"),
                            "status": started.get("status"),
                        }
                    )
                    blocked.add(ticker)
                except Exception as exc:
                    logger.exception("horizon review failed for %s", ticker)
                    actions.append({"ticker": ticker, "action": "error", "error": str(exc)})
            elif trigger == "horizon":
                actions.append({"ticker": ticker, "action": "horizon_deferred"})
            else:
                actions.append({"ticker": ticker, "action": "hold", "nearest_rule": nearest})

        await session.commit()
        event_type = "exit_review_completed" if scheduled else "exit_review_manual"
        await store.add_audit(
            session,
            event_type=event_type,
            payload={"date": day, "actions": actions},
        )
        return {"skipped": False, "date": day, "actions": actions}


async def exit_review_loop() -> None:
    """Weekday pass after the configured UTC hour. A restart the same day does not repeat it."""
    while True:
        try:
            settings = get_settings()
            now = datetime.now(UTC)
            hour = settings.exit_review_hour_utc
            if review_window_open(now, hour):
                factory = get_session_factory()
                async with factory() as session:
                    await review_exits(session, now=now, scheduled=True)
                delay = max(
                    60.0,
                    (next_review_at(datetime.now(UTC), hour) - datetime.now(UTC)).total_seconds(),
                )
            else:
                delay = max(
                    1.0,
                    (next_review_at(now, hour) - now).total_seconds(),
                )
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("exit review loop failed")
            await asyncio.sleep(60)
