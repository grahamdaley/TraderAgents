"""Paper-only session pass. Scans, researches a short list, and approves immediately.

The flag is off until AUTO_TRADE_ENABLED is set, and it stays disarmed in live mode.
A finished pass is not repeated the same New York day. A pass that stops before
the finished audit, and a live-mode skip, can both be tried again that day.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from trader import store
from trader.config import Settings, get_settings
from trader.db import get_engine, get_session_factory
from trader.exits import review_exits
from trader.scanner import load_universe, run_scan
from trader.services import get_book_snapshot, maybe_auto_approve, start_research_run

logger = logging.getLogger(__name__)

_NY = ZoneInfo("America/New_York")
_CONVICTION_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
_LOCK_NAMESPACE = 481_516_234
_autopilot_lock = asyncio.Lock()


def pass_already_finished(payload: dict[str, Any] | None) -> bool:
    """Only a finished audit closes the New York day."""
    return (payload or {}).get("phase") == "finished"


def autopilot_armed(settings: Settings | None = None) -> bool:
    """True only for an explicit paper flag with live orders still off."""
    settings = settings or get_settings()
    return bool(
        settings.auto_trade_enabled
        and settings.ibkr_trading_mode == "paper"
        and not settings.ibkr_live_orders_enabled
    )


def conviction_meets(conviction: str | None, minimum: str) -> bool:
    rank = _CONVICTION_RANK.get(str(conviction or "").upper())
    floor = _CONVICTION_RANK.get(str(minimum or "").upper())
    if rank is None or floor is None:
        return False
    return rank >= floor


def approval_skip_reason(
    *,
    state: dict[str, Any],
    buys_used: int,
    settings: Settings,
) -> str | None:
    """None means the awaiting proposal may be approved. Otherwise a skip reason."""
    if not autopilot_armed(settings):
        return "not_armed"
    thesis = (state or {}).get("pm_thesis") or {}
    proposal = (state or {}).get("trade_proposal") or {}
    side = str(proposal.get("side") or "").upper()
    order_type = str(proposal.get("order_type") or "").upper()
    if order_type != "LIMIT":
        return "not_limit"
    if side not in {"BUY", "SELL"}:
        return "side"
    if not conviction_meets(thesis.get("conviction"), settings.auto_min_conviction):
        return "conviction"
    if side == "BUY" and buys_used >= settings.auto_max_new_buys:
        return "buy_cap"
    return None


def select_research_names(
    candidates: list[dict[str, Any]],
    *,
    limit: int,
    held: set[str],
    blocked: set[str],
    restricted: set[str],
) -> list[dict[str, Any]]:
    """Rank order, at most one name per sector, skipping longs and names already in play."""
    if limit <= 0:
        return []
    ordered = sorted(
        candidates,
        key=lambda row: int(row.get("rank") or 10**6),
    )
    chosen: list[dict[str, Any]] = []
    sectors: set[str] = set()
    for row in ordered:
        ticker = str(row.get("ticker") or "").upper()
        if not ticker:
            continue
        if ticker in held or ticker in blocked or ticker in restricted:
            continue
        sector = str(row.get("sector") or "").strip().upper()
        # Missing sectors must not share one bucket, or the short list collapses.
        if sector and sector in sectors:
            continue
        chosen.append(row)
        if sector:
            sectors.add(sector)
        if len(chosen) >= limit:
            break
    return chosen


def _as_ny(now: datetime) -> datetime:
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    return now.astimezone(_NY)


def slot_on(now: datetime, minutes_after_open: int) -> datetime:
    local = _as_ny(now)
    open_at = local.replace(hour=9, minute=30, second=0, microsecond=0)
    return open_at + timedelta(minutes=max(0, int(minutes_after_open)))


def autopilot_window_open(now: datetime, minutes_after_open: int) -> bool:
    local = _as_ny(now)
    if local.weekday() >= 5:
        return False
    return local >= slot_on(local, minutes_after_open)


def next_autopilot_at(now: datetime, minutes_after_open: int) -> datetime:
    """Next weekday slot strictly after `now` once today's slot has passed."""
    local = _as_ny(now)
    slot = slot_on(local, minutes_after_open)
    if local < slot and slot.weekday() < 5:
        return slot
    day = local + timedelta(days=1)
    slot = slot_on(day, minutes_after_open)
    while slot.weekday() >= 5:
        day = day + timedelta(days=1)
        slot = slot_on(day, minutes_after_open)
    return slot


def _ny_day(now: datetime) -> str:
    return _as_ny(now).date().isoformat()


def _hold_tickers(runs: list[Any]) -> set[str]:
    found: set[str] = set()
    for run in runs:
        thesis = (getattr(run, "state_snapshot", None) or {}).get("pm_thesis") or {}
        if thesis.get("side") == "HOLD":
            found.add(str(run.ticker).upper())
    return found


def is_exit_rule_state(state: dict[str, Any] | None) -> bool:
    state = state or {}
    if str(state.get("run_kind") or "") == "horizon_review":
        return False
    if str(state.get("run_kind") or "") == "exit_rule":
        return True
    return bool(state.get("exit_reason"))


def proposal_expired(proposal: dict[str, Any] | None, now: datetime) -> bool:
    raw = (proposal or {}).get("expires_at")
    if not raw:
        return False
    if isinstance(raw, datetime):
        expires = raw
    else:
        text_value = str(raw).replace("Z", "+00:00")
        expires = datetime.fromisoformat(text_value)
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=UTC)
    moment = now if now.tzinfo is not None else now.replace(tzinfo=UTC)
    return moment > expires


def _ran_today(runs: list[Any], day: str) -> set[str]:
    found: set[str] = set()
    for run in runs:
        created = getattr(run, "created_at", None)
        if created is None:
            continue
        if _ny_day(created) == day:
            found.add(str(run.ticker).upper())
    return found


@asynccontextmanager
async def autopilot_db_lock(day: str):
    """Hold a Postgres advisory lock for this New York day.

    The lock sits on its own connection, so commits inside the pass do not
    release it. A second process gets False until this one unlocks or dies.
    """
    key = int(day.replace("-", ""))
    engine = get_engine()
    async with engine.connect() as conn:
        locked = await conn.scalar(
            text("SELECT pg_try_advisory_lock(:k1, :k2)"),
            {"k1": _LOCK_NAMESPACE, "k2": key},
        )
        await conn.commit()
        try:
            yield bool(locked)
        finally:
            if locked:
                await conn.execute(
                    text("SELECT pg_advisory_unlock(:k1, :k2)"),
                    {"k1": _LOCK_NAMESPACE, "k2": key},
                )
                await conn.commit()


async def _buys_already_used(session: AsyncSession, day: str) -> int:
    """Buy approvals already recorded today, so a retry does not raise the cap."""
    count = 0
    for row in await store.list_events(session, "auto_decision", limit=100):
        created = row.created_at
        if created is None or _ny_day(created) != day:
            continue
        payload = row.payload or {}
        if not str(payload.get("action") or "").startswith("approve"):
            continue
        if str(payload.get("side") or "").upper() == "BUY":
            count += 1
    return count


async def _approve_waiting(
    session: AsyncSession,
    *,
    run_id: str | None,
    state: dict[str, Any],
    buys_used: int,
    settings: Settings,
) -> dict[str, Any]:
    if not run_id:
        return {"approved": False, "reason": "missing_run", "buys_used": buys_used}
    return await maybe_auto_approve(
        session,
        run_id=run_id,
        state=state,
        buys_used=buys_used,
        settings=settings,
    )


async def run_autopilot_pass(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    """One New York session: exits, scan, research, immediate approve.

    The finished audit is written only after the attempt ends. A crash before
    that, or a live-mode skip, leaves the day open for another try.
    """
    async with _autopilot_lock:
        moment = now or datetime.now(UTC)
        settings = settings or get_settings()
        day = _ny_day(moment)
        if await store.autopilot_recorded(session, day):
            return {"skipped": True, "date": day, "reason": "already_ran", "actions": []}

        if not settings.auto_trade_enabled:
            return {"skipped": True, "date": day, "reason": "disabled", "actions": []}

        if not autopilot_armed(settings):
            await store.add_audit(
                session,
                event_type="autopilot_skipped",
                actor="autopilot",
                payload={
                    "date": day,
                    "reason": "refused unless paper mode with live orders disabled",
                    "trading_mode": settings.ibkr_trading_mode,
                    "live_orders_enabled": settings.ibkr_live_orders_enabled,
                },
            )
            return {"skipped": True, "date": day, "reason": "not_armed", "actions": []}

        async with autopilot_db_lock(day) as locked:
            if not locked:
                return {
                    "skipped": True,
                    "date": day,
                    "reason": "in_progress",
                    "actions": [],
                }
            if await store.autopilot_recorded(session, day):
                return {
                    "skipped": True,
                    "date": day,
                    "reason": "already_ran",
                    "actions": [],
                }

            return await _run_locked_pass(
                session, moment=moment, day=day, settings=settings
            )


async def _run_locked_pass(
    session: AsyncSession,
    *,
    moment: datetime,
    day: str,
    settings: Settings,
) -> dict[str, Any]:
    actions: list[dict[str, Any]] = []
    buys_used = await _buys_already_used(session, day)
    error: str | None = None
    try:
        await review_exits(
            session,
            now=moment,
            scheduled=True,
            include_horizon=False,
        )
        waiting = await store.list_runs(session, limit=200)
        for run in waiting:
            if getattr(run, "status", None) != "awaiting_human":
                continue
            state = getattr(run, "state_snapshot", None) or {}
            if not is_exit_rule_state(state):
                continue
            proposal = state.get("trade_proposal") or {}
            entry = {
                "ticker": getattr(run, "ticker", ""),
                "action": "exit_proposal",
                "run_id": getattr(run, "id", None),
                "status": "awaiting_human",
            }
            if proposal_expired(proposal, moment):
                actions.append({**entry, "auto": "expired"})
                continue
            try:
                decision = await _approve_waiting(
                    session,
                    run_id=getattr(run, "id", None),
                    state=state,
                    buys_used=buys_used,
                    settings=settings,
                )
            except Exception as exc:  # noqa: BLE001
                logger.exception("autopilot exit approve failed")
                actions.append({**entry, "auto": "error", "error": str(exc)})
                continue
            buys_used = int(decision["buys_used"])
            actions.append(
                {
                    **entry,
                    "auto": decision["reason"],
                    "final_status": decision.get("status"),
                }
            )

        held: set[str] = set()
        try:
            book = get_book_snapshot()
            held = {
                p.ticker.upper()
                for p in book.positions
                if getattr(p, "quantity", 0) > 0
            }
        except Exception as exc:  # noqa: BLE001
            logger.warning("autopilot scan continuing without book: %s", exc)
            actions.append({"action": "book_unavailable", "error": str(exc)})

        recent = await store.list_runs(session, limit=200)
        universe = load_universe()
        result = await asyncio.to_thread(
            run_scan,
            universe=universe,
            held_tickers=held,
            hold_tickers=_hold_tickers(recent),
        )
        scan = await store.save_scan(
            session,
            regime=result["regime"],
            universe_count=result["universe_count"],
            payload={"hot_sectors": result.get("hot_sectors") or []},
            candidates=result["candidates"],
        )
        actions.append(
            {
                "action": "scan",
                "scan_id": scan.id,
                "candidates": len(result["candidates"]),
            }
        )

        open_rows = await store.list_open_proposals(session)
        blocked = {row.ticker.upper() for row in open_rows}
        blocked |= _ran_today(recent, day)
        chosen = select_research_names(
            result["candidates"],
            limit=settings.auto_research_top_n,
            held=held,
            blocked=blocked,
            restricted=settings.restricted_set,
        )
        for row in chosen:
            ticker = str(row["ticker"]).upper()
            try:
                started = await start_research_run(session, ticker)
            except Exception as exc:  # noqa: BLE001
                logger.exception("autopilot research failed for %s", ticker)
                actions.append(
                    {"ticker": ticker, "action": "research_error", "error": str(exc)}
                )
                continue
            entry = {
                "ticker": ticker,
                "action": "research",
                "run_id": started.get("run_id"),
                "status": started.get("status"),
            }
            if started.get("status") == "awaiting_human":
                try:
                    decision = await _approve_waiting(
                        session,
                        run_id=started.get("run_id"),
                        state=started.get("state") or {},
                        buys_used=buys_used,
                        settings=settings,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.exception("autopilot approve failed for %s", ticker)
                    entry["auto"] = "error"
                    entry["error"] = str(exc)
                else:
                    buys_used = int(decision["buys_used"])
                    entry["auto"] = decision["reason"]
                    entry["final_status"] = decision.get("status")
            else:
                entry["auto"] = "no_proposal"
            actions.append(entry)
    except Exception as exc:  # noqa: BLE001
        logger.exception("autopilot pass failed")
        error = str(exc)

    await store.add_audit(
        session,
        event_type="autopilot_completed",
        actor="autopilot",
        payload={
            "date": day,
            "phase": "finished",
            "actions": actions,
            "buys": buys_used,
            "error": error,
        },
    )
    return {
        "skipped": False,
        "date": day,
        "reason": "finished",
        "actions": actions,
        "error": error,
    }


async def autopilot_loop() -> None:
    """Weekday pass after the US open. A finished pass is not repeated that day."""
    while True:
        try:
            settings = get_settings()
            now = datetime.now(UTC)
            minutes = settings.auto_trade_minutes_after_open
            if autopilot_window_open(now, minutes):
                factory = get_session_factory()
                async with factory() as session:
                    result = await run_autopilot_pass(
                        session, now=now, settings=settings
                    )
                if result.get("reason") == "in_progress":
                    delay = 30.0
                else:
                    delay = max(
                        60.0,
                        (
                            next_autopilot_at(datetime.now(UTC), minutes)
                            - datetime.now(UTC)
                        ).total_seconds(),
                    )
            else:
                delay = max(
                    1.0,
                    (next_autopilot_at(now, minutes) - now).total_seconds(),
                )
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("autopilot loop failed")
            await asyncio.sleep(60)
