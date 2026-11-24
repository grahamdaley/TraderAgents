"""First-class market specs for US and Hong Kong equity desks."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo

from trader.config import CONFIG_DIR

MarketId = Literal["us", "hk"]

_US_TICKER_RE = re.compile(r"^[A-Z0-9.\-]+$")
_HK_YAHOO_RE = re.compile(r"^(\d{1,5})\.HK$")
_HK_NUMERIC_RE = re.compile(r"^\d{1,5}$")

# HKEX Main Board tick table (price → minimum price variation).
# Boundaries match "from X to Y" / "over X to Y" in the exchange schedule.
_SEHK_TICK_BANDS: tuple[tuple[float, float], ...] = (
    (0.25, 0.001),
    (0.50, 0.005),
    (10.0, 0.01),
    (20.0, 0.02),
    (100.0, 0.05),
    (200.0, 0.10),
    (500.0, 0.20),
    (1000.0, 0.50),
    (2000.0, 1.0),
    (5000.0, 2.0),
)


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
        # Yahoo no longer serves ^VHSI; scanners derive a VIX-like level from HSI.
        regime_vol_symbol="",
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


def sehk_tick_size(price: float) -> float:
    """HKEX Main Board minimum price variation for ``price``."""
    p = abs(float(price))
    if p <= 0:
        return 0.001
    for upper, tick in _SEHK_TICK_BANDS:
        if p <= upper:
            return tick
    return 5.0


def tick_size_for_price(price: float, market: MarketId | str) -> float:
    """Venue tick size used when rounding limit prices."""
    mid = parse_market(str(market))
    if mid == "hk":
        return sehk_tick_size(price)
    # US listed equities ≥ $1 trade in pennies; sub-dollar uses $0.0001.
    return 0.0001 if abs(float(price)) < 1.0 else 0.01


def round_to_tick(
    price: float,
    market: MarketId | str,
    *,
    tick: float | None = None,
) -> float:
    """Round a limit price to a valid venue tick (nearest, half-up).

    ``tick`` overrides the market table — use IBKR ``minTick`` when available.
    """
    raw = float(price)
    if not math.isfinite(raw) or raw <= 0:
        raise ValueError(f"limit price must be a positive finite number; got {price!r}")
    size = float(tick) if tick is not None and float(tick) > 0 else tick_size_for_price(
        raw, market
    )
    # Quantize via Decimal so SEHK bands like 0.10 do not leave float dust
    # (e.g. 137.14 → 137.10, not 137.1000000002).
    quant = Decimal(str(size))
    stepped = (Decimal(str(raw)) / quant).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(stepped * quant)


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
