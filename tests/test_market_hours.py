from datetime import datetime
from zoneinfo import ZoneInfo

from trader.market_hours import us_equity_session_open

_NY = ZoneInfo("America/New_York")


def test_us_equity_session_open_weekday_rth():
    noon = datetime(2026, 11, 16, 12, 0, tzinfo=_NY)  # Monday
    assert us_equity_session_open(noon) is True


def test_us_equity_session_closed_overnight():
    evening = datetime(2026, 11, 16, 20, 0, tzinfo=_NY)
    assert us_equity_session_open(evening) is False


def test_us_equity_session_closed_weekend():
    saturday = datetime(2026, 11, 14, 12, 0, tzinfo=_NY)
    assert us_equity_session_open(saturday) is False
