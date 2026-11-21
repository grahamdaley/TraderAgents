from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from trader.markets import MarketId, get_market, parse_market

_NY = ZoneInfo("America/New_York")
_HK = ZoneInfo("Asia/Hong_Kong")
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)
_HK_MORNING_OPEN = time(9, 30)
_HK_MORNING_CLOSE = time(12, 0)
_HK_AFTERNOON_OPEN = time(13, 0)
_HK_AFTERNOON_CLOSE = time(16, 0)


def equity_session_open(market: MarketId | str, now: datetime | None = None) -> bool:
    """True during regular equity hours for the given market."""
    mid = parse_market(str(market))
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if mid == "us":
        return _us_session_open(when)
    return _hk_session_open(when)


def us_equity_session_open(now: datetime | None = None) -> bool:
    """True during regular US equity hours (Mon–Fri 09:30–16:00 America/New_York)."""
    return equity_session_open("us", now)


def hk_equity_session_open(now: datetime | None = None) -> bool:
    """True during HKEX continuous trading (morning + afternoon, Asia/Hong_Kong)."""
    return equity_session_open("hk", now)


def market_local_day(market: MarketId | str, now: datetime | None = None) -> str:
    """YYYY-MM-DD in the market's timezone."""
    spec = get_market(market)
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    return when.astimezone(spec.timezone).date().isoformat()


def minutes_since_open(market: MarketId | str, now: datetime | None = None) -> int | None:
    """Minutes since the morning open on a weekday session; None if not a session day."""
    spec = get_market(market)
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone(spec.timezone)
    if local.weekday() >= 5:
        return None
    open_dt = local.replace(hour=9, minute=30, second=0, microsecond=0)
    return int((local - open_dt).total_seconds() // 60)


def _us_session_open(when: datetime) -> bool:
    local = when.astimezone(_NY)
    if local.weekday() >= 5:
        return False
    t = local.time()
    return _RTH_OPEN <= t < _RTH_CLOSE


def _hk_session_open(when: datetime) -> bool:
    local = when.astimezone(_HK)
    if local.weekday() >= 5:
        return False
    t = local.time()
    morning = _HK_MORNING_OPEN <= t < _HK_MORNING_CLOSE
    afternoon = _HK_AFTERNOON_OPEN <= t < _HK_AFTERNOON_CLOSE
    return morning or afternoon
