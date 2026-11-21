"""First-class market specs for US and Hong Kong equity desks."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from trader.config import CONFIG_DIR

MarketId = Literal["us", "hk"]

_US_TICKER_RE = re.compile(r"^[A-Z0-9.\-]+$")
_HK_YAHOO_RE = re.compile(r"^(\d{1,5})\.HK$")
_HK_NUMERIC_RE = re.compile(r"^\d{1,5}$")


@dataclass(frozen=True)
class MarketSpec:
    id: MarketId
    currency: str
    ib_exchange: str
    yahoo_suffix: str
    timezone: ZoneInfo
    universe_path: Path
    regime_vol_symbol: str
    regime_index_symbol: str
    default_ticker: str
    default_lot_size: int
    label: str


MARKETS: dict[MarketId, MarketSpec] = {
    "us": MarketSpec(
        id="us",
        currency="USD",
        ib_exchange="SMART",
        yahoo_suffix="",
        timezone=ZoneInfo("America/New_York"),
        universe_path=CONFIG_DIR / "universe.csv",
        regime_vol_symbol="^VIX",
        regime_index_symbol="^GSPC",
        default_ticker="AAPL",
        default_lot_size=1,
        label="US",
    ),
    "hk": MarketSpec(
        id="hk",
        currency="HKD",
        ib_exchange="SEHK",
        yahoo_suffix=".HK",
        timezone=ZoneInfo("Asia/Hong_Kong"),
        universe_path=CONFIG_DIR / "universe-hk.csv",
        regime_vol_symbol="^VHSI",
        regime_index_symbol="^HSI",
        default_ticker="0700.HK",
        default_lot_size=100,
        label="Hong Kong",
    ),
}


def parse_market(value: str | None, *, default: MarketId = "us") -> MarketId:
    raw = (value or default).strip().lower()
    if raw not in MARKETS:
        raise ValueError(f"Unknown market {value!r}; expected us or hk")
    return raw  # type: ignore[return-value]


def get_market(market: MarketId | str) -> MarketSpec:
    return MARKETS[parse_market(str(market))]


def normalize_ticker(ticker: str, market: MarketId | str) -> str:
    """Canonical Yahoo-form ticker for research and DB storage."""
    spec = get_market(market)
    raw = ticker.strip().upper()
    if spec.id == "us":
        cleaned = raw.replace(" ", "-")
        if not _US_TICKER_RE.fullmatch(cleaned):
            raise ValueError("US ticker must be letters, digits, or a class separator")
        return cleaned

    if _HK_YAHOO_RE.fullmatch(raw):
        digits = _HK_YAHOO_RE.match(raw).group(1)  # type: ignore[union-attr]
        return f"{int(digits):04d}.HK"
    if _HK_NUMERIC_RE.fullmatch(raw):
        return f"{int(raw):04d}.HK"
    raise ValueError("HK ticker must be numeric (e.g. 0700 or 0700.HK)")


def yahoo_symbol(ticker: str, market: MarketId | str) -> str:
    return normalize_ticker(ticker, market)


def ibkr_symbol(ticker: str, market: MarketId | str) -> str:
    """IBKR Stock.symbol for the venue (no .HK suffix; leading zeros stripped)."""
    spec = get_market(market)
    canonical = normalize_ticker(ticker, market)
    if spec.id == "us":
        return canonical
    digits = canonical.removesuffix(".HK")
    return str(int(digits))


def tickers_match(left: str, right: str, market: MarketId | str = "us") -> bool:
    """Compare research and broker ticker forms for the same instrument."""
    mid = parse_market(str(market))
    a = (left or "").strip().upper()
    b = (right or "").strip().upper()
    if not a or not b:
        return False
    if a == b:
        return True
    if mid == "us":
        return a.replace("-", ".") == b.replace("-", ".") or a.replace(".", "-") == b.replace(
            ".", "-"
        )
    try:
        return normalize_ticker(a, mid) == normalize_ticker(b, mid)
    except ValueError:
        return False


def display_ticker_from_ibkr(symbol: str, market: MarketId | str, currency: str | None = None) -> str:
    """Map an IBKR contract symbol into Yahoo/DB form for the desk."""
    mid = parse_market(str(market))
    raw = (symbol or "").strip().upper()
    if mid == "hk" or (currency or "").upper() == "HKD":
        try:
            return normalize_ticker(raw, "hk")
        except ValueError:
            return raw
    return raw


def round_to_lot(quantity: float, lot_size: int) -> float:
    """Round share quantity down to a whole board-lot multiple."""
    lot = max(int(lot_size or 1), 1)
    if quantity <= 0:
        return 0.0
    lots = int(quantity) // lot
    return float(lots * lot)


def lot_size_for(ticker: str, market: MarketId | str, *, fallback: int | None = None) -> int:
    """Look up board lot from the committed universe; fall back to market default."""
    import csv

    spec = get_market(market)
    default = fallback if fallback is not None else spec.default_lot_size
    try:
        canonical = normalize_ticker(ticker, market)
    except ValueError:
        return default
    path = spec.universe_path
    if not path.exists():
        return default
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            if str(row.get("ticker") or "").strip().upper() != canonical:
                continue
            raw = (row.get("lot_size") or "").strip()
            try:
                value = int(float(raw)) if raw else default
            except (TypeError, ValueError):
                value = default
            return max(value, 1)
    return default
