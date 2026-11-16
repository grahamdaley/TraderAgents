from __future__ import annotations

from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")
_RTH_OPEN = time(9, 30)
_RTH_CLOSE = time(16, 0)


def us_equity_session_open(now: datetime | None = None) -> bool:
    """True during regular US equity hours (Mon–Fri 09:30–16:00 America/New_York)."""
    when = now or datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone(_NY)
    if local.weekday() >= 5:
        return False
    t = local.time()
    return _RTH_OPEN <= t < _RTH_CLOSE
