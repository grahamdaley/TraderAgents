from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from contextlib import asynccontextmanager

from trader.autopilot import (
    approval_skip_reason,
    autopilot_armed,
    autopilot_window_open,
    next_autopilot_at,
    pass_already_finished,
    run_autopilot_pass,
    select_research_names,
)
from trader.config import Settings
from trader.services import decision_event_type

_NY = ZoneInfo("America/New_York")


def _paper(**overrides) -> Settings:
    values = {
        "AUTO_TRADE_ENABLED": True,
        "IBKR_TRADING_MODE": "paper",
        "IBKR_LIVE_ORDERS_ENABLED": False,
        "AUTO_MIN_CONVICTION": "MEDIUM",
        "AUTO_MAX_NEW_BUYS": 3,
        "AUTO_RESEARCH_TOP_N": 3,
    }
    values.update(overrides)
    return Settings(**values)


def _proposal(conviction: str, side: str = "BUY") -> dict:
    return {
        "pm_thesis": {"conviction": conviction, "side": side},
        "trade_proposal": {"side": side, "order_type": "LIMIT"},
    }


def test_autopilot_armed_only_for_paper():
    assert autopilot_armed(_paper(AUTO_TRADE_ENABLED=False)) is False
    assert autopilot_armed(_paper()) is True
    assert (
        autopilot_armed(
            _paper(IBKR_TRADING_MODE="live", IBKR_LIVE_ORDERS_ENABLED=True)
        )
        is False
    )
    assert autopilot_armed(_paper(IBKR_LIVE_ORDERS_ENABLED=True)) is False
    assert Settings(AUTO_TRADE_ENABLED="false").auto_trade_enabled is False


def test_conviction_floor_and_buy_cap():
    paper = _paper()
    assert approval_skip_reason(state=_proposal("MEDIUM"), buys_used=0, settings=paper) is None
    assert (
        approval_skip_reason(state=_proposal("LOW"), buys_used=0, settings=paper)
        == "conviction"
    )
    assert approval_skip_reason(state=_proposal("HIGH"), buys_used=0, settings=paper) is None
    assert (
        approval_skip_reason(state=_proposal("HIGH", "SELL"), buys_used=3, settings=paper)
        is None
    )
    assert (
        approval_skip_reason(state=_proposal("HIGH"), buys_used=3, settings=paper)
        == "buy_cap"
    )
    assert decision_event_type("autopilot") == "auto_decision"
    assert decision_event_type("operator") == "human_decision"


def test_select_research_names_skips_and_spreads_sectors():
    candidates = [
        {"ticker": "HELD", "sector": "XLK", "rank": 1},
        {"ticker": "AAA", "sector": "XLK", "rank": 2},
        {"ticker": "AAB", "sector": "XLK", "rank": 3},
        {"ticker": "BUSY", "sector": "XLF", "rank": 4},
        {"ticker": "BBB", "sector": "XLE", "rank": 5},
        {"ticker": "NOPE", "sector": "XLV", "rank": 6},
        {"ticker": "CCC", "sector": "XLI", "rank": 7},
    ]
    chosen = select_research_names(
        candidates,
        limit=3,
        held={"HELD"},
        blocked={"BUSY"},
        restricted={"NOPE"},
    )
    assert [row["ticker"] for row in chosen] == ["AAA", "BBB", "CCC"]

    blanks = select_research_names(
        [
            {"ticker": "ONE", "sector": "", "rank": 1},
            {"ticker": "TWO", "sector": None, "rank": 2},
            {"ticker": "THREE", "sector": "XLK", "rank": 3},
        ],
        limit=3,
        held=set(),
        blocked=set(),
        restricted=set(),
    )
    assert [row["ticker"] for row in blanks] == ["ONE", "TWO", "THREE"]


def test_only_a_finished_pass_closes_the_day():
    assert pass_already_finished({"phase": "finished", "date": "2026-11-16"}) is True
    assert pass_already_finished({"phase": "started", "date": "2026-11-16"}) is False
    assert pass_already_finished({"date": "2026-11-16"}) is False


def test_schedule_skips_weekends_and_waits_for_the_open():
    saturday = datetime(2026, 11, 14, 12, tzinfo=_NY)
    assert autopilot_window_open(saturday, 5) is False
    monday = next_autopilot_at(saturday, 5)
    assert monday.weekday() == 0
    assert (monday.hour, monday.minute) == (9, 35)

    early = datetime(2026, 11, 16, 9, 34, tzinfo=_NY)
    assert autopilot_window_open(early, 5) is False
    assert next_autopilot_at(early, 5) == datetime(2026, 11, 16, 9, 35, tzinfo=_NY)

    opened = datetime(2026, 11, 16, 9, 35, tzinfo=_NY)
    assert autopilot_window_open(opened, 5) is True

    friday_afternoon = datetime(2026, 11, 20, 16, tzinfo=_NY)
    following = next_autopilot_at(friday_afternoon, 5)
    assert following.weekday() == 0
    assert following.day == 23
    assert (following.hour, following.minute) == (9, 35)


async def test_pass_approves_medium_and_skips_low(monkeypatch):
    audits: list[dict] = []
    approved: list[str] = []
    reviews: list[dict] = []

    async def recorded(session, day):
        return False

    async def add_audit(session, **kwargs):
        audits.append(kwargs)

    async def review_exits(session, **kwargs):
        reviews.append(kwargs)
        return {"actions": []}

    async def list_runs(session, limit=50):
        return [
            SimpleNamespace(
                id="run-EXIT",
                ticker="OLD",
                status="awaiting_human",
                created_at=datetime(2026, 11, 16, 14, tzinfo=_NY),
                state_snapshot={
                    "run_kind": "exit_rule",
                    "exit_reason": "below sma",
                    "pm_thesis": {"conviction": "HIGH", "side": "SELL"},
                    "trade_proposal": {
                        "side": "SELL",
                        "order_type": "LIMIT",
                        "expires_at": "2026-11-16T23:00:00+00:00",
                    },
                },
            )
        ]

    async def list_events(session, event_type, limit=100):
        return []

    @asynccontextmanager
    async def db_lock(day):
        yield True

    async def list_open_proposals(session):
        return []

    async def save_scan(session, **kwargs):
        return SimpleNamespace(id="scan-1")

    def load_universe():
        return [{"ticker": "AAA", "sector": "XLK", "name": "A"}]

    def run_scan(**kwargs):
        return {
            "regime": "neutral",
            "universe_count": 2,
            "hot_sectors": [],
            "candidates": [
                {"ticker": "AAA", "sector": "XLK", "rank": 1, "score": 2},
                {"ticker": "BBB", "sector": "XLF", "rank": 2, "score": 1},
            ],
        }

    class _Book:
        positions: list = []

    async def start_research_run(session, ticker, **kwargs):
        conviction = "LOW" if ticker == "BBB" else "MEDIUM"
        return {
            "run_id": f"run-{ticker}",
            "status": "awaiting_human",
            "state": _proposal(conviction),
        }

    async def resume_with_decision(session, *, run_id, request):
        approved.append(run_id)
        assert request.operator == "autopilot"
        assert request.action == "approve"
        return {"status": "submitted"}

    monkeypatch.setattr("trader.store.autopilot_recorded", recorded)
    monkeypatch.setattr("trader.store.add_audit", add_audit)
    monkeypatch.setattr("trader.store.list_events", list_events)
    monkeypatch.setattr("trader.autopilot.autopilot_db_lock", db_lock)
    monkeypatch.setattr("trader.autopilot.review_exits", review_exits)
    monkeypatch.setattr("trader.autopilot.load_universe", load_universe)
    monkeypatch.setattr("trader.autopilot.run_scan", run_scan)
    monkeypatch.setattr("trader.autopilot.get_book_snapshot", lambda: _Book())
    monkeypatch.setattr("trader.store.list_runs", list_runs)
    monkeypatch.setattr("trader.store.list_open_proposals", list_open_proposals)
    monkeypatch.setattr("trader.store.save_scan", save_scan)
    monkeypatch.setattr("trader.autopilot.start_research_run", start_research_run)
    monkeypatch.setattr("trader.services.resume_with_decision", resume_with_decision)

    result = await run_autopilot_pass(
        session=object(),
        now=datetime(2026, 11, 16, 14, 40, tzinfo=_NY),
        settings=_paper(),
    )

    assert result["skipped"] is False
    assert reviews and reviews[0]["include_horizon"] is False
    assert approved == ["run-EXIT", "run-AAA"]
    research = [row for row in result["actions"] if row.get("action") == "research"]
    by_ticker = {row["ticker"]: row for row in research}
    assert by_ticker["AAA"]["auto"] == "approved"
    assert by_ticker["BBB"]["auto"] == "conviction"
    assert any(item.get("payload", {}).get("phase") == "finished" for item in audits)
    assert not any(item.get("payload", {}).get("phase") == "started" for item in audits)
    exits = [row for row in result["actions"] if row.get("action") == "exit_proposal"]
    assert exits and exits[0]["auto"] == "approved"


async def test_lock_busy_does_not_claim_the_day(monkeypatch):
    audits: list[dict] = []

    async def recorded(session, day):
        return False

    async def add_audit(session, **kwargs):
        audits.append(kwargs)

    @asynccontextmanager
    async def db_lock(day):
        yield False

    monkeypatch.setattr("trader.store.autopilot_recorded", recorded)
    monkeypatch.setattr("trader.store.add_audit", add_audit)
    monkeypatch.setattr("trader.autopilot.autopilot_db_lock", db_lock)

    result = await run_autopilot_pass(
        session=object(),
        now=datetime(2026, 11, 16, 14, 40, tzinfo=_NY),
        settings=_paper(),
    )

    assert result["reason"] == "in_progress"
    assert audits == []


async def test_live_mode_skips_without_research(monkeypatch):
    audits: list[dict] = []

    async def recorded(session, day):
        return False

    async def add_audit(session, **kwargs):
        audits.append(kwargs)

    async def review_exits(session, **kwargs):
        raise AssertionError("live mode must not review exits")

    monkeypatch.setattr("trader.store.autopilot_recorded", recorded)
    monkeypatch.setattr("trader.store.add_audit", add_audit)
    monkeypatch.setattr("trader.autopilot.review_exits", review_exits)

    result = await run_autopilot_pass(
        session=object(),
        now=datetime(2026, 11, 16, 14, 40, tzinfo=_NY),
        settings=_paper(IBKR_TRADING_MODE="live", IBKR_LIVE_ORDERS_ENABLED=True),
    )

    assert result["reason"] == "not_armed"
    assert audits[0]["event_type"] == "autopilot_skipped"
    assert audits[0]["actor"] == "autopilot"
