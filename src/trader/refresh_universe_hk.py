"""Rebuild config/universe-hk.csv from HSI + Hang Seng TECH constituents.

Scans read only the committed snapshot. Run by hand, review the diff, and commit:

    python -m trader.refresh_universe_hk

If live constituent pages are unreachable, falls back to the embedded seed list
and still writes a valid CSV for offline/dev use.
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

import httpx

from trader.config import CONFIG_DIR

# Seed: overlapping HSI + Hang Seng TECH names (Yahoo .HK form).
# lot_size defaults are typical board lots; refresh may overwrite when known.
SEED_ROWS: list[dict[str, str]] = [
    {"ticker": "0005.HK", "name": "HSBC HOLDINGS", "sector": "FINANCIALS", "lot_size": "400"},
    {"ticker": "0011.HK", "name": "HANG SENG BANK", "sector": "FINANCIALS", "lot_size": "100"},
    {"ticker": "0388.HK", "name": "HKEX", "sector": "FINANCIALS", "lot_size": "100"},
    {"ticker": "0939.HK", "name": "CCB", "sector": "FINANCIALS", "lot_size": "1000"},
    {"ticker": "1398.HK", "name": "ICBC", "sector": "FINANCIALS", "lot_size": "1000"},
    {"ticker": "2318.HK", "name": "PING AN", "sector": "FINANCIALS", "lot_size": "500"},
    {"ticker": "2628.HK", "name": "CHINA LIFE", "sector": "FINANCIALS", "lot_size": "1000"},
    {"ticker": "1299.HK", "name": "AIA", "sector": "FINANCIALS", "lot_size": "200"},
    {"ticker": "2388.HK", "name": "BOC HONG KONG", "sector": "FINANCIALS", "lot_size": "500"},
    {"ticker": "3968.HK", "name": "CM BANK", "sector": "FINANCIALS", "lot_size": "500"},
    {"ticker": "0001.HK", "name": "CKH HOLDINGS", "sector": "CONGLOMERATES", "lot_size": "500"},
    {"ticker": "0002.HK", "name": "CLP HOLDINGS", "sector": "UTILITIES", "lot_size": "500"},
    {"ticker": "0003.HK", "name": "HK & CHINA GAS", "sector": "UTILITIES", "lot_size": "1000"},
    {"ticker": "0006.HK", "name": "POWER ASSETS", "sector": "UTILITIES", "lot_size": "500"},
    {"ticker": "0016.HK", "name": "SHK PPT", "sector": "PROPERTIES", "lot_size": "1000"},
    {"ticker": "0066.HK", "name": "MTR CORPORATION", "sector": "TRANSPORT", "lot_size": "500"},
    {"ticker": "0823.HK", "name": "LINK REIT", "sector": "PROPERTIES", "lot_size": "100"},
    {"ticker": "1109.HK", "name": "CHINA RES LAND", "sector": "PROPERTIES", "lot_size": "2000"},
    {"ticker": "0688.HK", "name": "CHINA OVERSEAS", "sector": "PROPERTIES", "lot_size": "2000"},
    {"ticker": "0012.HK", "name": "HENDERSON LAND", "sector": "PROPERTIES", "lot_size": "1000"},
    {"ticker": "0017.HK", "name": "NEW WORLD DEV", "sector": "PROPERTIES", "lot_size": "1000"},
    {"ticker": "0101.HK", "name": "HANG LUNG PPT", "sector": "PROPERTIES", "lot_size": "1000"},
    {"ticker": "1038.HK", "name": "CKI HOLDINGS", "sector": "UTILITIES", "lot_size": "500"},
    {"ticker": "0941.HK", "name": "CHINA MOBILE", "sector": "TELECOM", "lot_size": "500"},
    {"ticker": "0762.HK", "name": "CHINA UNICOM", "sector": "TELECOM", "lot_size": "2000"},
    {"ticker": "0728.HK", "name": "CHINA TELECOM", "sector": "TELECOM", "lot_size": "2000"},
    {"ticker": "0883.HK", "name": "CNOOC", "sector": "ENERGY", "lot_size": "1000"},
    {"ticker": "0386.HK", "name": "SINOPEC CORP", "sector": "ENERGY", "lot_size": "2000"},
    {"ticker": "0857.HK", "name": "PETROCHINA", "sector": "ENERGY", "lot_size": "2000"},
    {"ticker": "0267.HK", "name": "CITIC", "sector": "CONGLOMERATES", "lot_size": "1000"},
    {"ticker": "1928.HK", "name": "SANDS CHINA", "sector": "CONSUMER", "lot_size": "400"},
    {"ticker": "2020.HK", "name": "ANTA SPORTS", "sector": "CONSUMER", "lot_size": "200"},
    {"ticker": "2319.HK", "name": "MENGNIU DAIRY", "sector": "CONSUMER", "lot_size": "1000"},
    {"ticker": "0291.HK", "name": "CHINA RES BEER", "sector": "CONSUMER", "lot_size": "2000"},
    {"ticker": "1044.HK", "name": "HENGAN INT'L", "sector": "CONSUMER", "lot_size": "500"},
    {"ticker": "0669.HK", "name": "TECHTRONIC IND", "sector": "INDUSTRIALS", "lot_size": "500"},
    {"ticker": "1177.HK", "name": "SINO BIOPHARM", "sector": "HEALTHCARE", "lot_size": "1000"},
    {"ticker": "2269.HK", "name": "WUXI BIO", "sector": "HEALTHCARE", "lot_size": "500"},
    {"ticker": "1093.HK", "name": "CSPC PHARMA", "sector": "HEALTHCARE", "lot_size": "2000"},
    {"ticker": "6160.HK", "name": "BEIGENE", "sector": "HEALTHCARE", "lot_size": "50"},
    {"ticker": "0968.HK", "name": "XINYI SOLAR", "sector": "INDUSTRIALS", "lot_size": "2000"},
    {"ticker": "0700.HK", "name": "TENCENT", "sector": "INFOTECH", "lot_size": "100"},
    {"ticker": "9988.HK", "name": "ALIBABA", "sector": "INFOTECH", "lot_size": "100"},
    {"ticker": "3690.HK", "name": "MEITUAN", "sector": "INFOTECH", "lot_size": "100"},
    {"ticker": "1810.HK", "name": "XIAOMI", "sector": "INFOTECH", "lot_size": "200"},
    {"ticker": "9618.HK", "name": "JD.COM", "sector": "INFOTECH", "lot_size": "50"},
    {"ticker": "1024.HK", "name": "KUAISHOU", "sector": "INFOTECH", "lot_size": "100"},
    {"ticker": "9888.HK", "name": "BAIDU", "sector": "INFOTECH", "lot_size": "50"},
    {"ticker": "9999.HK", "name": "NETEASE", "sector": "INFOTECH", "lot_size": "100"},
    {"ticker": "9626.HK", "name": "BILIBILI", "sector": "INFOTECH", "lot_size": "20"},
    {"ticker": "0981.HK", "name": "SMIC", "sector": "INFOTECH", "lot_size": "500"},
    {"ticker": "2382.HK", "name": "SUNNY OPTICAL", "sector": "INFOTECH", "lot_size": "1000"},
    {"ticker": "2015.HK", "name": "LI AUTO", "sector": "INFOTECH", "lot_size": "100"},
    {"ticker": "9868.HK", "name": "XPENG", "sector": "INFOTECH", "lot_size": "100"},
    {"ticker": "9866.HK", "name": "NIO", "sector": "INFOTECH", "lot_size": "100"},
    {"ticker": "1211.HK", "name": "BYD COMPANY", "sector": "INFOTECH", "lot_size": "500"},
    {"ticker": "0992.HK", "name": "LENOVO GROUP", "sector": "INFOTECH", "lot_size": "2000"},
    {"ticker": "0763.HK", "name": "ZTE", "sector": "INFOTECH", "lot_size": "2000"},
    {"ticker": "0285.HK", "name": "BYD ELECTRONIC", "sector": "INFOTECH", "lot_size": "1000"},
    {"ticker": "1347.HK", "name": "HUA HONG SEMI", "sector": "INFOTECH", "lot_size": "500"},
    {"ticker": "3888.HK", "name": "KINGSOFT", "sector": "INFOTECH", "lot_size": "1000"},
    {"ticker": "0268.HK", "name": "KINGDEE INT'L", "sector": "INFOTECH", "lot_size": "1000"},
    {"ticker": "0522.HK", "name": "ASMPT", "sector": "INFOTECH", "lot_size": "500"},
    {"ticker": "0200.HK", "name": "MELCO RESORTS", "sector": "CONSUMER", "lot_size": "1000"},
    {"ticker": "1929.HK", "name": "CHOW TAI FOOK", "sector": "CONSUMER", "lot_size": "200"},
    {"ticker": "6862.HK", "name": "HAIDILAO", "sector": "CONSUMER", "lot_size": "500"},
    {"ticker": "9961.HK", "name": "TRIP.COM", "sector": "CONSUMER", "lot_size": "50"},
    {"ticker": "6690.HK", "name": "HAIER SMARTHOME", "sector": "CONSUMER", "lot_size": "400"},
    {"ticker": "6618.HK", "name": "JD HEALTH", "sector": "HEALTHCARE", "lot_size": "50"},
]

MIN_EQUITIES = 40

WIKI_HSI = "https://en.wikipedia.org/wiki/Hang_Seng_Index"
WIKI_HSTECH = "https://en.wikipedia.org/wiki/Hang_Seng_Tech_Index"

# Rough industry keyword → HSICS-style sector label.
_NAME_SECTOR_HINTS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"BANK|INSUR|AIA|PING AN|LIFE|FINANC|EXCHANGE|HKEX", re.I), "FINANCIALS"),
    (re.compile(r"MOBILE|TELECOM|UNICOM", re.I), "TELECOM"),
    (re.compile(r"PETRO|OIL|CNOOC|GAS|ENERGY|POWER|UTILIT|CLP", re.I), "ENERGY"),
    (re.compile(r"PROPERT|LAND|REAL|REIT|HENDERSON|HANG LUNG|SHK", re.I), "PROPERTIES"),
    (re.compile(r"BIO|PHARM|HEALTH|BEIGENE|WUXI", re.I), "HEALTHCARE"),
    (
        re.compile(
            r"TENCENT|ALIBABA|MEITUAN|XIAOMI|JD\.|KUAISHOU|BAIDU|NETEASE|BILIBILI|"
            r"SEMIC|SMIC|OPTICAL|LENOVO|ZTE|KINGSOFT|ASMPT|XPENG|NIO|LI AUTO|BYD|"
            r"TECH|SEMI|SOFTWARE|CLOUD",
            re.I,
        ),
        "INFOTECH",
    ),
    (re.compile(r"SPORTS|DAIRY|BEER|SANDS|MELCO|RESORT|TRAVEL|TRIP", re.I), "CONSUMER"),
    (re.compile(r"MTR|TRANSPORT|AIRPORT", re.I), "TRANSPORT"),
]


def normalize_hk_ticker(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw or "")
    if not digits:
        return None
    try:
        return f"{int(digits):04d}.HK"
    except ValueError:
        return None


def sector_for_name(name: str, *, default: str = "OTHER") -> str:
    for pattern, sector in _NAME_SECTOR_HINTS:
        if pattern.search(name or ""):
            return sector
    return default


def _parse_wiki_tables(html: str) -> list[dict[str, str]]:
    """Pull code/name pairs from Wikipedia constituent tables."""
    rows: list[dict[str, str]] = []
    # Match table cells that look like 0001 / 0700.HK / 9988
    pattern = re.compile(
        r"<td[^>]*>\s*(?:<a[^>]*>)?\s*(\d{1,5})(?:\.HK)?\s*(?:</a>)?\s*</td>"
        r".*?<td[^>]*>\s*(?:<a[^>]*>)?\s*([^<]{2,80})",
        re.I | re.S,
    )
    seen: set[str] = set()
    for match in pattern.finditer(html):
        ticker = normalize_hk_ticker(match.group(1))
        name = re.sub(r"\s+", " ", match.group(2)).strip()
        if not ticker or ticker in seen or not name:
            continue
        seen.add(ticker)
        rows.append(
            {
                "ticker": ticker,
                "name": name.upper()[:80],
                "sector": sector_for_name(name),
                "lot_size": "100",
            }
        )
    return rows


def fetch_wiki(url: str) -> str:
    response = httpx.get(
        url,
        headers={"User-Agent": "research-desk/universe-refresh-hk"},
        follow_redirects=True,
        timeout=60,
    )
    response.raise_for_status()
    return response.text


def merge_constituents(*groups: list[dict[str, str]]) -> list[dict[str, str]]:
    by_ticker: dict[str, dict[str, str]] = {}
    for group in groups:
        for row in group:
            ticker = row["ticker"]
            existing = by_ticker.get(ticker)
            if existing is None:
                by_ticker[ticker] = dict(row)
                continue
            # Prefer richer sector / name when replacing OTHER or empty.
            if existing.get("sector") == "OTHER" and row.get("sector") != "OTHER":
                existing["sector"] = row["sector"]
            if len(row.get("name") or "") > len(existing.get("name") or ""):
                existing["name"] = row["name"]
            if row.get("lot_size") and existing.get("lot_size") in {"", "100"}:
                existing["lot_size"] = row["lot_size"]
    rows = list(by_ticker.values())
    rows.sort(key=lambda item: item["ticker"])
    return rows


def write_universe(path: Path, rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle, fieldnames=["ticker", "name", "sector", "lot_size"]
        )
        writer.writeheader()
        writer.writerows(rows)


def refresh_universe_hk(path: Path | None = None) -> int:
    destination = path or (CONFIG_DIR / "universe-hk.csv")
    live_rows: list[dict[str, str]] = []
    try:
        hsi = _parse_wiki_tables(fetch_wiki(WIKI_HSI))
        hstech = _parse_wiki_tables(fetch_wiki(WIKI_HSTECH))
        live_rows = merge_constituents(hsi, hstech)
    except Exception as exc:  # noqa: BLE001
        print(f"live fetch failed ({exc}); using seed list")
        live_rows = []

    seed = [{**row} for row in SEED_ROWS]
    # Overlay seed lot sizes / sectors onto live names when present.
    seed_by = {row["ticker"]: row for row in seed}
    if live_rows and len(live_rows) >= MIN_EQUITIES:
        for row in live_rows:
            hint = seed_by.get(row["ticker"])
            if hint:
                row["lot_size"] = hint.get("lot_size") or row.get("lot_size") or "100"
                if row.get("sector") in {"", "OTHER"}:
                    row["sector"] = hint.get("sector") or "OTHER"
                if not row.get("name"):
                    row["name"] = hint.get("name") or row["ticker"]
        rows = live_rows
    else:
        rows = merge_constituents(seed)

    if len(rows) < MIN_EQUITIES:
        raise ValueError(f"only {len(rows)} equities; refusing to replace the snapshot")
    write_universe(destination, rows)
    return len(rows)


def main() -> None:
    count = refresh_universe_hk()
    print(f"wrote {count} names to {CONFIG_DIR / 'universe-hk.csv'}")


if __name__ == "__main__":
    main()
