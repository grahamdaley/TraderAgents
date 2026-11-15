"""Operator-started scan. No model calls.

Pass 1 ranks the committed universe from daily bars. Pass 2 adds earnings,
estimate revisions, and headlines for a short list only.
"""

from __future__ import annotations

import logging
from datetime import date, datetime
from typing import Any

import pandas as pd
import yfinance as yf

from trader.config import CONFIG_DIR
from trader.tools.market_data import (
    MarketDataTools,
    get_market_data_tools,
    indicators_from_closes,
)

logger = logging.getLogger(__name__)

SECTOR_ETFS = ["XLK", "XLY", "XLP", "XLV", "XLF", "XLE", "XLI", "XLB", "XLRE", "XLU", "XLC"]
SHORTLIST = 25
RESULT_LIMIT = 20
VOL_PENALTY_ABOVE = 0.45
GAP_PCT = 0.04
EARNINGS_WINDOW_DAYS = 3
DEBT_TO_EQUITY_MAX = 150.0


def load_universe(path=None) -> list[dict[str, str]]:
    source = path or (CONFIG_DIR / "universe.csv")
    if not source.exists():
        raise FileNotFoundError(
            f"{source} is missing. Run `python -m trader.refresh_universe` and commit the file."
        )
    frame = pd.read_csv(source, dtype=str).fillna("")
    rows = []
    for record in frame.to_dict(orient="records"):
        ticker = str(record.get("ticker") or "").strip().upper()
        sector = str(record.get("sector") or "").strip().upper()
        if not ticker or not sector:
            continue
        rows.append(
            {
                "ticker": ticker,
                "name": str(record.get("name") or "").strip(),
                "sector": sector,
            }
        )
    if not rows:
        raise ValueError(f"{source} has no tickers")
    return rows


def classify_regime(vix: float | None, spx_1m: float | None) -> str:
    if (vix is not None and vix >= 20) or (spx_1m is not None and spx_1m <= -0.03):
        return "risk_off"
    if (vix is not None and vix <= 16) and (spx_1m is not None and spx_1m > 0):
        return "risk_on"
    return "neutral"


def _inverse_vol(vol: float | None) -> float:
    if vol is None or vol < 0:
        return 0.7
    return 1.0 / (1.0 + vol)


def score_name(
    *,
    return_3m: float | None,
    realized_vol: float | None,
    trend_ok: bool,
    regime: str,
    sector_hot: bool,
    attention: bool,
    book_new_sector: bool,
    hold_recheck: bool,
) -> tuple[float, list[str]]:
    """Point score for one name. Regime changes the momentum/volatility mix."""
    momentum = return_3m or 0.0
    calm = _inverse_vol(realized_vol)
    if regime == "risk_on":
        score = momentum + 0.15 * calm
    elif regime == "risk_off":
        score = 0.3 * momentum + 0.7 * calm
    else:
        score = 0.6 * momentum + 0.4 * calm

    signals: list[str] = []
    if trend_ok:
        score += 0.25
        signals.append("trend")
    if realized_vol is not None and realized_vol > VOL_PENALTY_ABOVE:
        score -= 0.20
    if sector_hot:
        score += 0.20
        signals.append("sector")
    if attention:
        score += 0.15
        signals.append("attention")
    if book_new_sector:
        score += 0.10
        signals.append("book")
    if hold_recheck and trend_ok:
        score += 0.20
        if "book" not in signals:
            signals.append("book")
    return score, signals


def pass2_adjustment(
    *,
    trend_ok: bool,
    gap_and_volume: bool,
    earnings_confirmed: bool,
    revisions_up: bool | None,
) -> tuple[float, list[str]]:
    """Earnings and revisions. A missing revision field abstains."""
    delta = 0.0
    signals: list[str] = []
    if gap_and_volume and earnings_confirmed:
        delta += 0.35
        signals.append("earnings")
    if trend_ok and revisions_up is True:
        delta += 0.25
        signals.append("revisions")
    return delta, signals


def earnings_near_gap(gap_on: date, earnings: list[date], window: int = EARNINGS_WINDOW_DAYS) -> bool:
    return any(abs((gap_on - day).days) <= window for day in earnings)


def fundamentals_support_drift(ratios: dict[str, Any] | None) -> bool:
    ratios = ratios or {}
    free_cashflow = ratios.get("free_cashflow")
    if free_cashflow is None or float(free_cashflow) <= 0:
        return False
    debt = ratios.get("debt_to_equity")
    return not (debt is not None and float(debt) >= DEBT_TO_EQUITY_MAX)


def _as_date(value: Any) -> date | None:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if hasattr(value, "date"):
        try:
            return value.date()
        except Exception:  # noqa: BLE001
            return None
    return None


def bar_features(bars: pd.DataFrame) -> dict[str, Any] | None:
    """Indicators plus a 10-session volume spike and an earnings-style gap."""
    frame = _normalize_ohlcv(bars)
    if frame is None or "Close" not in frame:
        return None
    closes = frame["Close"].dropna()
    if len(closes) < 63:
        return None
    indicators = indicators_from_closes(closes)
    sma20 = indicators.get("sma20")
    sma50 = indicators.get("sma50")
    rsi = indicators.get("rsi14")
    trend_ok = (
        sma20 is not None
        and sma50 is not None
        and sma20 > sma50
        and rsi is not None
        and 40 <= rsi <= 70
    )

    volume = frame["Volume"] if "Volume" in frame else pd.Series(dtype=float)
    opened = frame["Open"] if "Open" in frame else frame["Close"]
    prev_close = frame["Close"].shift(1)
    gap = opened / prev_close - 1
    baseline = volume.rolling(20).mean().shift(1)
    spike = volume >= (2 * baseline)
    gap_and_volume = (gap >= GAP_PCT) & spike.fillna(False)
    recent_gap = gap_and_volume.tail(10)
    gap_date = None
    if recent_gap.any():
        gap_date = _as_date(recent_gap[recent_gap].index[-1])
    attention = bool(spike.fillna(False).tail(10).any())
    return {
        **indicators,
        "trend_ok": trend_ok,
        "attention": attention,
        "gap_and_volume": gap_date is not None,
        "gap_date": gap_date.isoformat() if gap_date else None,
    }


def _normalize_ohlcv(bars: pd.DataFrame) -> pd.DataFrame | None:
    if bars is None or bars.empty:
        return None
    frame = bars.copy()
    frame.columns = [str(col).title() for col in frame.columns]
    if "Close" not in frame and "Adj Close" in frame:
        frame["Close"] = frame["Adj Close"]
    return frame.dropna(how="all")


def revisions_are_up(payload: Any) -> bool | None:
    """True when 30-day upward revisions exceed downward ones. None if absent."""
    if payload is None:
        return None
    if isinstance(payload, dict):
        up = payload.get("upLast30days")
        down = payload.get("downLast30days")
        if up is None or down is None:
            return None
        try:
            return float(up) > float(down)
        except (TypeError, ValueError):
            return None
    if isinstance(payload, pd.DataFrame):
        up = _frame_total(payload, "upLast30days")
        down = _frame_total(payload, "downLast30days")
        if up is None or down is None:
            return None
        return up > down
    return None


def _frame_total(frame: pd.DataFrame, key: str) -> float | None:
    if frame.empty:
        return None
    key_l = key.lower()
    series = None
    for idx in frame.index:
        if str(idx).lower() == key_l:
            series = frame.loc[idx]
            break
    if series is None:
        for col in frame.columns:
            if str(col).lower() == key_l:
                series = frame[col]
                break
    if series is None:
        return None
    nums = pd.to_numeric(series, errors="coerce")
    if isinstance(nums, pd.Series):
        if not bool(nums.notna().any()):
            return None
        return float(nums.sum(skipna=True))
    if pd.isna(nums):
        return None
    return float(nums)


def earnings_dates_from_frame(frame: Any) -> list[date]:
    if frame is None or not isinstance(frame, pd.DataFrame) or frame.empty:
        return []
    found: list[date] = []
    for idx in frame.index:
        day = _as_date(idx)
        if day is not None:
            found.append(day)
    return found


def relative_strength(etf: dict[str, Any], market: dict[str, Any]) -> float:
    return 0.5 * (
        ((etf.get("return_1m") or 0.0) - (market.get("return_1m") or 0.0))
        + ((etf.get("return_3m") or 0.0) - (market.get("return_3m") or 0.0))
    )


def leading_sectors(etf_features: dict[str, dict[str, Any]], market: dict[str, Any], n: int = 3) -> list[str]:
    ranked = sorted(
        etf_features,
        key=lambda ticker: relative_strength(etf_features[ticker], market),
        reverse=True,
    )
    return ranked[:n]


def build_reason(row: dict[str, Any]) -> str:
    signals = set(row.get("signals") or [])
    bits: list[str] = []
    ret = row.get("return_3m")
    if "trend" in signals and ret is not None:
        bits.append(f"trend up, 3-month return {ret:.1%}")
    elif "trend" in signals:
        bits.append("trend up")
    if "sector" in signals:
        bits.append(f"sector {row.get('sector')} is leading")
    if "attention" in signals:
        bits.append("volume spike in the last 10 sessions")
    if "book" in signals and row.get("book_new_sector"):
        bits.append("adds a sector the book does not hold")
    if "book" in signals and row.get("hold_recheck"):
        bits.append("prior hold now meets the trend rule")
    if "earnings" in signals:
        bits.append("post-earnings gap with positive free cash flow and modest debt")
    if "revisions" in signals:
        bits.append("upward 30-day estimate revisions")
    headlines = row.get("headlines") or []
    if headlines:
        bits.append(f"headline: {headlines[0]}")
    return "; ".join(bits)[:500]


def rank_universe(
    names: list[dict[str, Any]],
    *,
    regime: str,
    hot_sectors: set[str],
    held_sectors: set[str],
    hold_tickers: set[str],
    open_tickers: set[str],
) -> list[dict[str, Any]]:
    """Pass-1 ranking. Open positions are left out; they belong to exit review."""
    apply_book = bool(held_sectors)
    scored: list[dict[str, Any]] = []
    for name in names:
        ticker = name["ticker"].upper()
        if ticker in open_tickers:
            continue
        features = name.get("features") or {}
        if not features:
            continue
        sector = name.get("sector") or ""
        hold_recheck = ticker in hold_tickers
        book_new = apply_book and sector not in held_sectors
        score, signals = score_name(
            return_3m=features.get("return_3m"),
            realized_vol=features.get("realized_vol_20d"),
            trend_ok=bool(features.get("trend_ok")),
            regime=regime,
            sector_hot=sector in hot_sectors,
            attention=bool(features.get("attention")),
            book_new_sector=book_new,
            hold_recheck=hold_recheck,
        )
        if not signals and not hold_recheck:
            continue
        scored.append(
            {
                "ticker": ticker,
                "name": name.get("name") or "",
                "sector": sector,
                "score": score,
                "signals": signals,
                "return_3m": features.get("return_3m"),
                "trend_ok": bool(features.get("trend_ok")),
                "gap_and_volume": bool(features.get("gap_and_volume")),
                "gap_date": features.get("gap_date"),
                "book_new_sector": book_new,
                "hold_recheck": hold_recheck and bool(features.get("trend_ok")),
                "headlines": [],
            }
        )
    scored.sort(key=lambda row: row["score"], reverse=True)
    return scored


def finalize_candidates(scored: list[dict[str, Any]], limit: int = RESULT_LIMIT) -> list[dict[str, Any]]:
    """Top names, plus any prior HOLD that now passes the trend rule."""
    ranked = [row for row in scored if row.get("signals")]
    ranked.sort(key=lambda row: row["score"], reverse=True)
    chosen = ranked[:limit]
    seen = {row["ticker"] for row in chosen}
    for row in ranked:
        if row.get("hold_recheck") and row["ticker"] not in seen:
            chosen.append(row)
            seen.add(row["ticker"])
    for index, row in enumerate(chosen, start=1):
        row["rank"] = index
        row["reason"] = build_reason(row)
        row["score"] = round(float(row["score"]), 4)
    return chosen


def _split_download(data: pd.DataFrame, tickers: list[str]) -> dict[str, pd.DataFrame]:
    out: dict[str, pd.DataFrame] = {}
    if data is None or data.empty:
        return out
    if isinstance(data.columns, pd.MultiIndex):
        level0 = set(data.columns.get_level_values(0))
        if "Close" in level0 or "Adj Close" in level0:
            data = data.swaplevel(0, 1, axis=1)
            data = data.sort_index(axis=1)
        available = set(data.columns.get_level_values(0))
        for ticker in tickers:
            if ticker not in available:
                continue
            frame = _normalize_ohlcv(data[ticker])
            if frame is not None:
                out[ticker] = frame
        return out
    frame = _normalize_ohlcv(data)
    if frame is not None and len(tickers) == 1:
        out[tickers[0]] = frame
    return out


def download_bars(tickers: list[str]) -> dict[str, pd.DataFrame]:
    if not tickers:
        return {}
    data = yf.download(
        tickers,
        period="1y",
        group_by="ticker",
        auto_adjust=True,
        threads=True,
        progress=False,
    )
    return _split_download(data, tickers)


def _enrich_shortlist(rows: list[dict[str, Any]], tools: MarketDataTools) -> None:
    for row in rows:
        ticker = row["ticker"]
        revisions_up: bool | None = None
        earnings_ok = False
        headlines: list[str] = []
        try:
            stock = yf.Ticker(ticker)
            revisions_up = revisions_are_up(getattr(stock, "eps_revisions", None))
        except Exception as exc:  # noqa: BLE001
            logger.warning("revisions unavailable for %s: %s", ticker, exc)
            revisions_up = None
        if row.get("gap_and_volume") and row.get("gap_date"):
            try:
                dates = earnings_dates_from_frame(yf.Ticker(ticker).earnings_dates)
                gap_on = date.fromisoformat(row["gap_date"])
                fundamentals = tools.get_fundamentals(ticker)
                earnings_ok = earnings_near_gap(gap_on, dates) and fundamentals_support_drift(
                    fundamentals.get("ratios")
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("earnings check failed for %s: %s", ticker, exc)
                earnings_ok = False
        try:
            news = tools.get_news(ticker, limit=5)
            headlines = [a["title"] for a in news.get("articles") or [] if a.get("title")][:3]
        except Exception as exc:  # noqa: BLE001
            logger.warning("news unavailable for %s: %s", ticker, exc)
        delta, extra = pass2_adjustment(
            trend_ok=bool(row.get("trend_ok")),
            gap_and_volume=bool(row.get("gap_and_volume")),
            earnings_confirmed=earnings_ok,
            revisions_up=revisions_up,
        )
        row["score"] = float(row["score"]) + delta
        row["signals"] = list(dict.fromkeys([*row["signals"], *extra]))
        row["headlines"] = headlines


def run_scan(
    *,
    universe: list[dict[str, str]] | None = None,
    held_tickers: set[str] | None = None,
    hold_tickers: set[str] | None = None,
    macro: dict[str, Any] | None = None,
    tools: MarketDataTools | None = None,
) -> dict[str, Any]:
    """Full scan. Network I/O stays in this function so scoring tests stay offline."""
    universe = universe if universe is not None else load_universe()
    tools = tools or get_market_data_tools()
    held_tickers = {t.upper() for t in (held_tickers or set())}
    hold_tickers = {t.upper() for t in (hold_tickers or set())}
    sector_by_ticker = {row["ticker"]: row["sector"] for row in universe}
    held_sectors = {sector_by_ticker[t] for t in held_tickers if t in sector_by_ticker}

    symbols = [row["ticker"] for row in universe]
    extra = [*SECTOR_ETFS, "^GSPC"]
    bars = download_bars(symbols + extra)
    macro = macro if macro is not None else tools.get_macro_snapshot()
    regime = classify_regime(macro.get("vix"), macro.get("spx_1m_return"))

    market_bars = bars.get("^GSPC")
    market = bar_features(market_bars) if market_bars is not None else {}
    if not market:
        market = {"return_1m": macro.get("spx_1m_return") or 0.0, "return_3m": 0.0}
    etf_features = {}
    for etf in SECTOR_ETFS:
        features = bar_features(bars[etf]) if etf in bars else None
        if features:
            etf_features[etf] = features
    hot = set(leading_sectors(etf_features, market)) if etf_features else set()

    prepared = []
    for row in universe:
        features = bar_features(bars[row["ticker"]]) if row["ticker"] in bars else None
        prepared.append({**row, "features": features or {}})

    scored = rank_universe(
        prepared,
        regime=regime,
        hot_sectors=hot,
        held_sectors=held_sectors,
        hold_tickers=hold_tickers,
        open_tickers=held_tickers,
    )
    shortlist = []
    seen: set[str] = set()
    for row in scored:
        if len(shortlist) >= SHORTLIST:
            break
        shortlist.append(row)
        seen.add(row["ticker"])
    for row in scored:
        if row.get("hold_recheck") and row["ticker"] not in seen:
            shortlist.append(row)
            seen.add(row["ticker"])
    _enrich_shortlist(shortlist, tools)
    # Names outside the shortlist keep pass-1 scores.
    by_ticker = {row["ticker"]: row for row in shortlist}
    merged = [by_ticker.get(row["ticker"], row) for row in scored]
    candidates = finalize_candidates(merged)
    return {
        "regime": regime,
        "universe_count": len(universe),
        "hot_sectors": sorted(hot),
        "candidates": candidates,
    }
