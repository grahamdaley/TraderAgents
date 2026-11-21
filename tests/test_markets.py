from datetime import datetime
from zoneinfo import ZoneInfo

from trader.market_hours import equity_session_open, hk_equity_session_open, market_local_day
from trader.markets import (
    ibkr_symbol,
    normalize_ticker,
    round_to_lot,
    tickers_match,
)


_HK = ZoneInfo("Asia/Hong_Kong")


def test_normalize_hk_ticker_padding():
    assert normalize_ticker("700", "hk") == "0700.HK"
    assert normalize_ticker("0700.HK", "hk") == "0700.HK"
    assert normalize_ticker("9988.hk", "hk") == "9988.HK"


def test_ibkr_symbol_strips_hk_suffix():
    assert ibkr_symbol("0700.HK", "hk") == "700"
    assert ibkr_symbol("AAPL", "us") == "AAPL"


def test_round_to_lot():
    assert round_to_lot(350, 100) == 300.0
    assert round_to_lot(99, 100) == 0.0
    assert round_to_lot(10.9, 1) == 10.0


def test_tickers_match_hk_forms():
    assert tickers_match("0700.HK", "700", "hk")
    assert tickers_match("AAPL", "AAPL", "us")


def test_hk_session_morning_and_afternoon():
    morning = datetime(2026, 11, 16, 10, 0, tzinfo=_HK)  # Monday
    lunch = datetime(2026, 11, 16, 12, 30, tzinfo=_HK)
    afternoon = datetime(2026, 11, 16, 14, 0, tzinfo=_HK)
    assert hk_equity_session_open(morning) is True
    assert equity_session_open("hk", lunch) is False
    assert hk_equity_session_open(afternoon) is True


def test_hk_session_closed_weekend():
    saturday = datetime(2026, 11, 14, 10, 0, tzinfo=_HK)
    assert hk_equity_session_open(saturday) is False


def test_market_local_day():
    utc = datetime(2026, 11, 16, 16, 0, tzinfo=ZoneInfo("UTC"))
    # 16:00 UTC = 00:00 next day HKT
    assert market_local_day("hk", utc) == "2026-11-17"
    assert market_local_day("us", utc).startswith("2026-11-16")
