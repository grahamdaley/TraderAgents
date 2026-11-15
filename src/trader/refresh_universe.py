"""Rebuild config/universe.csv from the iShares S&P 100 holdings file.

Scans read only the committed snapshot. Run this by hand, review the diff,
and commit it when membership changes:

    python -m trader.refresh_universe
"""

from __future__ import annotations

import csv
import io
from pathlib import Path

import httpx

from trader.config import CONFIG_DIR

OEF_HOLDINGS_URL = (
    "https://www.ishares.com/us/products/239723/ishares-s-p-100-etf/latest-holdings.csv"
)

# iShares sector name -> SPDR sector ETF used by the scanner.
SECTOR_TO_SPDR = {
    "Information Technology": "XLK",
    "Consumer Discretionary": "XLY",
    "Consumer Staples": "XLP",
    "Health Care": "XLV",
    "Financials": "XLF",
    "Energy": "XLE",
    "Industrials": "XLI",
    "Materials": "XLB",
    "Real Estate": "XLRE",
    "Utilities": "XLU",
    "Communication": "XLC",
    "Communication Services": "XLC",
}

MIN_EQUITIES = 90


def normalize_ticker(raw: str) -> str:
    """Yahoo form: 'BRK B' and 'BRK.B' both become 'BRK-B'."""
    return raw.strip().upper().replace(".", "-").replace(" ", "-").replace("/", "-")


def parse_holdings_csv(text: str, *, min_equities: int = MIN_EQUITIES) -> list[dict[str, str]]:
    """Keep equity rows and map sectors. Refuse a short or unknown file."""
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith("Ticker,")), None)
    if start is None:
        raise ValueError("holdings file has no Ticker header")

    reader = csv.DictReader(io.StringIO("\n".join(lines[start:])))
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for row in reader:
        asset = (row.get("Asset Class") or "").strip()
        if asset != "Equity":
            continue
        ticker = normalize_ticker(row.get("Ticker") or "")
        if not ticker or ticker in {"-", "--"}:
            continue
        sector_name = (row.get("Sector") or "").strip()
        spdr = SECTOR_TO_SPDR.get(sector_name)
        if spdr is None:
            raise ValueError(f"unknown sector {sector_name!r} for {ticker}")
        if ticker in seen:
            continue
        seen.add(ticker)
        rows.append(
            {
                "ticker": ticker,
                "name": (row.get("Name") or "").strip(),
                "sector": spdr,
            }
        )

    if len(rows) < min_equities:
        raise ValueError(f"only {len(rows)} equities; refusing to replace the snapshot")
    rows.sort(key=lambda item: item["ticker"])
    return rows


def write_universe(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["ticker", "name", "sector"])
        writer.writeheader()
        writer.writerows(rows)


def fetch_holdings(url: str = OEF_HOLDINGS_URL) -> str:
    response = httpx.get(
        url,
        headers={"User-Agent": "research-desk/universe-refresh"},
        follow_redirects=True,
        timeout=60,
    )
    response.raise_for_status()
    return response.text


def refresh_universe(path: Path | None = None) -> int:
    destination = path or (CONFIG_DIR / "universe.csv")
    rows = parse_holdings_csv(fetch_holdings())
    write_universe(destination, rows)
    return len(rows)


def main() -> None:
    count = refresh_universe()
    print(f"wrote {count} names to {CONFIG_DIR / 'universe.csv'}")


if __name__ == "__main__":
    main()
