from __future__ import annotations

import logging
from datetime import datetime, timezone
from functools import lru_cache
from typing import Any

import pandas as pd
import yfinance as yf

from trader.schemas import Citation

logger = logging.getLogger(__name__)


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


class MarketDataTools:
    """Yahoo Finance research tools. Execution quotes come from IBKR."""

    def get_price_history(self, ticker: str, period: str = "1y") -> dict[str, Any]:
        t = ticker.upper()
        hist = yf.Ticker(t).history(period=period)
        if hist.empty:
            return {"ticker": t, "bars": [], "error": "no price history"}
        bars = []
        for idx, row in hist.tail(252).iterrows():
            bars.append(
                {
                    "date": idx.date().isoformat(),
                    "open": _safe_float(row.get("Open")),
                    "high": _safe_float(row.get("High")),
                    "low": _safe_float(row.get("Low")),
                    "close": _safe_float(row.get("Close")),
                    "volume": _safe_float(row.get("Volume")),
                }
            )
        return {
            "ticker": t,
            "bars": bars,
            "as_of": datetime.now(timezone.utc).isoformat(),
            "citation": Citation(
                source_id=f"yf-history-{t}",
                label=f"Yahoo Finance daily bars for {t}",
                as_of=datetime.now(timezone.utc),
            ).model_dump(mode="json"),
        }

    def compute_indicators(self, ticker: str) -> dict[str, Any]:
        history = self.get_price_history(ticker)
        bars = history.get("bars") or []
        if len(bars) < 30:
            return {
                "ticker": ticker.upper(),
                "indicators": {},
                "error": "insufficient history",
            }
        closes = pd.Series([b["close"] for b in bars if b.get("close") is not None])
        sma20 = closes.rolling(20).mean().iloc[-1]
        sma50 = closes.rolling(50).mean().iloc[-1] if len(closes) >= 50 else None
        delta = closes.diff()
        gain = delta.clip(lower=0).rolling(14).mean()
        loss = (-delta.clip(upper=0)).rolling(14).mean()
        rs = gain / loss.replace(0, pd.NA)
        rsi = 100 - (100 / (1 + rs))
        ret = closes.pct_change().dropna()
        vol = float(ret.tail(20).std() * (252**0.5)) if len(ret) >= 20 else None
        last = float(closes.iloc[-1])
        indicators = {
            "last_close": last,
            "sma20": _safe_float(sma20),
            "sma50": _safe_float(sma50),
            "rsi14": _safe_float(rsi.iloc[-1]),
            "realized_vol_20d": vol,
            "return_1m": _safe_float(closes.iloc[-1] / closes.iloc[-21] - 1)
            if len(closes) >= 21
            else None,
            "return_3m": _safe_float(closes.iloc[-1] / closes.iloc[-63] - 1)
            if len(closes) >= 63
            else None,
        }
        return {
            "ticker": ticker.upper(),
            "indicators": indicators,
            "citation": Citation(
                source_id=f"yf-indicators-{ticker.upper()}",
                label=f"Computed indicators for {ticker.upper()}",
                as_of=datetime.now(timezone.utc),
            ).model_dump(mode="json"),
        }

    def get_fundamentals(self, ticker: str) -> dict[str, Any]:
        t = ticker.upper()
        stock = yf.Ticker(t)
        info = stock.info or {}
        ratios = {
            "trailing_pe": _safe_float(info.get("trailingPE")),
            "forward_pe": _safe_float(info.get("forwardPE")),
            "price_to_book": _safe_float(info.get("priceToBook")),
            "profit_margin": _safe_float(info.get("profitMargins")),
            "operating_margin": _safe_float(info.get("operatingMargins")),
            "roe": _safe_float(info.get("returnOnEquity")),
            "debt_to_equity": _safe_float(info.get("debtToEquity")),
            "current_ratio": _safe_float(info.get("currentRatio")),
            "free_cashflow": _safe_float(info.get("freeCashflow")),
            "revenue_growth": _safe_float(info.get("revenueGrowth")),
            "earnings_growth": _safe_float(info.get("earningsGrowth")),
            "market_cap": _safe_float(info.get("marketCap")),
        }

        def _frame_snapshot(frame: pd.DataFrame | None, n: int = 4) -> dict[str, Any]:
            if frame is None or frame.empty:
                return {}
            cols = list(frame.columns)[:n]
            out: dict[str, Any] = {}
            for col in cols:
                key = str(col.date()) if hasattr(col, "date") else str(col)
                out[key] = {
                    str(idx): _safe_float(frame.loc[idx, col])
                    for idx in list(frame.index)[:12]
                }
            return out

        income = _frame_snapshot(getattr(stock, "financials", None))
        balance = _frame_snapshot(getattr(stock, "balance_sheet", None))
        cashflow = _frame_snapshot(getattr(stock, "cashflow", None))
        return {
            "ticker": t,
            "ratios": ratios,
            "income_statement": income,
            "balance_sheet": balance,
            "cash_flow": cashflow,
            "citation": Citation(
                source_id=f"yf-fundamentals-{t}",
                label=f"Yahoo Finance fundamentals for {t}",
                as_of=datetime.now(timezone.utc),
            ).model_dump(mode="json"),
        }

    def get_news(self, ticker: str, limit: int = 12) -> dict[str, Any]:
        t = ticker.upper()
        stock = yf.Ticker(t)
        raw = []
        try:
            raw = stock.news or []
        except Exception as exc:  # noqa: BLE001
            logger.warning("news fetch failed for %s: %s", t, exc)
        articles = []
        for item in raw[:limit]:
            content = item.get("content") if isinstance(item.get("content"), dict) else {}
            title = item.get("title") or content.get("title") or ""
            publisher = (
                item.get("publisher")
                or (content.get("provider") or {}).get("displayName")
                or ""
            )
            click = content.get("clickThroughUrl") or {}
            if not isinstance(click, dict):
                click = {}
            articles.append(
                {
                    "title": title,
                    "publisher": publisher,
                    "link": item.get("link") or click.get("url"),
                    "published": item.get("providerPublishTime")
                    or content.get("pubDate"),
                }
            )
        return {
            "ticker": t,
            "articles": articles,
            "citation": Citation(
                source_id=f"yf-news-{t}",
                label=f"Yahoo Finance news for {t}",
                as_of=datetime.now(timezone.utc),
            ).model_dump(mode="json"),
        }

    def get_macro_snapshot(self) -> dict[str, Any]:
        symbols = {"^VIX": "vix", "^GSPC": "spx", "^TNX": "tnx"}
        values: dict[str, float | None] = {}
        for symbol, key in symbols.items():
            try:
                hist = yf.Ticker(symbol).history(period="5d")
                values[key] = (
                    _safe_float(hist["Close"].iloc[-1]) if not hist.empty else None
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("macro fetch failed for %s: %s", symbol, exc)
                values[key] = None
        spx_hist = yf.Ticker("^GSPC").history(period="3mo")
        spx_1m = None
        if not spx_hist.empty and len(spx_hist) >= 21:
            closes = spx_hist["Close"]
            spx_1m = _safe_float(closes.iloc[-1] / closes.iloc[-21] - 1)
        return {
            "vix": values.get("vix"),
            "spx": values.get("spx"),
            "tnx": values.get("tnx"),
            "spx_1m_return": spx_1m,
            "citation": Citation(
                source_id="yf-macro",
                label="Yahoo Finance macro snapshot",
                as_of=datetime.now(timezone.utc),
            ).model_dump(mode="json"),
        }


@lru_cache
def get_market_data_tools() -> MarketDataTools:
    return MarketDataTools()
