from datetime import UTC, date, datetime, timedelta

import pandas as pd

from trader.exits import classify_exit, next_review_at, review_window_open
from trader.graph import (
    build_graph,
    exit_thesis_node,
    initial_state,
    invoke_graph_locked,
    route_after_begin,
)
from trader.policy import PolicyRejection, build_trade_proposal
from trader.positions import average_entry, clamp_horizon_days
from trader.refresh_universe import parse_holdings_csv
from trader.scanner import (
    bar_features,
    classify_regime,
    earnings_near_gap,
    finalize_candidates,
    fundamentals_support_drift,
    pass2_adjustment,
    rank_universe,
    revisions_are_up,
    score_name,
)
from trader.schemas import BookSnapshot, PmThesis, PositionView, RiskNarrative, Side


def _features(ret, vol, trend=True, attention=False, gap=False):
    return {
        "return_3m": ret,
        "realized_vol_20d": vol,
        "trend_ok": trend,
        "attention": attention,
        "gap_and_volume": gap,
        "gap_date": None,
        "rsi14": 55,
        "sma20": 110,
        "sma50": 100,
    }


def test_bar_features_reads_trend_and_a_volume_spike():
    index = pd.bdate_range("2026-07-01", periods=80)
    close = pd.Series((100 + pd.Series(range(80), dtype=float) * 0.4).to_numpy(), index=index)
    frame = pd.DataFrame(
        {
            "Open": close,
            "High": close + 1,
            "Low": close - 1,
            "Close": close,
            "Volume": 1_000_000,
        }
    )
    frame.iloc[-2, frame.columns.get_loc("Volume")] = 4_000_000
    features = bar_features(frame)
    assert features is not None
    assert features["sma20"] > features["sma50"]
    assert features["attention"] is True
    assert features["return_3m"] > 0


def test_regime_weights_favor_momentum_then_calm():
    hot = score_name(
        return_3m=0.25,
        realized_vol=0.40,
        trend_ok=True,
        regime="risk_on",
        sector_hot=False,
        attention=False,
        book_new_sector=False,
        hold_recheck=False,
    )[0]
    calm = score_name(
        return_3m=0.02,
        realized_vol=0.08,
        trend_ok=True,
        regime="risk_on",
        sector_hot=False,
        attention=False,
        book_new_sector=False,
        hold_recheck=False,
    )[0]
    assert hot > calm

    hot_off = score_name(
        return_3m=0.25,
        realized_vol=0.40,
        trend_ok=True,
        regime="risk_off",
        sector_hot=False,
        attention=False,
        book_new_sector=False,
        hold_recheck=False,
    )[0]
    calm_off = score_name(
        return_3m=0.02,
        realized_vol=0.08,
        trend_ok=True,
        regime="risk_off",
        sector_hot=False,
        attention=False,
        book_new_sector=False,
        hold_recheck=False,
    )[0]
    assert calm_off > hot_off


def test_classify_regime_thresholds():
    assert classify_regime(22, 0.01) == "risk_off"
    assert classify_regime(12, -0.04) == "risk_off"
    assert classify_regime(14, 0.02) == "risk_on"
    assert classify_regime(18, 0.01) == "neutral"


def test_rank_excludes_open_positions_and_keeps_hold_recheck():
    names = [
        {"ticker": "AAA", "name": "A", "sector": "XLK", "features": _features(0.1, 0.2)},
        {"ticker": "BBB", "name": "B", "sector": "XLF", "features": _features(0.05, 0.2)},
        {
            "ticker": "HOLD",
            "name": "H",
            "sector": "XLV",
            "features": _features(0.01, 0.2, trend=True),
        },
    ]
    ranked = rank_universe(
        names,
        regime="neutral",
        hot_sectors={"XLK"},
        held_sectors={"XLF"},
        hold_tickers={"HOLD"},
        open_tickers={"AAA"},
    )
    tickers = [row["ticker"] for row in ranked]
    assert "AAA" not in tickers
    assert "HOLD" in tickers
    hold = next(row for row in ranked if row["ticker"] == "HOLD")
    assert hold["hold_recheck"] is True
    assert "book" in hold["signals"]
    bbb = next(row for row in ranked if row["ticker"] == "BBB")
    assert "book" not in bbb["signals"]


def test_earnings_gap_filter():
    gap = date(2026, 11, 10)
    assert earnings_near_gap(gap, [date(2026, 11, 12)])
    assert not earnings_near_gap(gap, [date(2026, 10, 1)])
    assert fundamentals_support_drift({"free_cashflow": 10, "debt_to_equity": 40})
    assert not fundamentals_support_drift({"free_cashflow": -1, "debt_to_equity": 10})
    assert not fundamentals_support_drift({"free_cashflow": 10, "debt_to_equity": 180})
    delta, signals = pass2_adjustment(
        trend_ok=True,
        gap_and_volume=True,
        earnings_confirmed=True,
        revisions_up=True,
    )
    assert delta > 0
    assert signals == ["earnings", "revisions"]
    abstain, extra = pass2_adjustment(
        trend_ok=True,
        gap_and_volume=True,
        earnings_confirmed=False,
        revisions_up=None,
    )
    assert abstain == 0
    assert extra == []


def test_revisions_frame_and_dict():
    assert revisions_are_up({"upLast30days": 3, "downLast30days": 1}) is True
    assert revisions_are_up({"upLast30days": 1, "downLast30days": 4}) is False
    assert revisions_are_up({}) is None
    frame = pd.DataFrame({"0q": [4, 1]}, index=["upLast30days", "downLast30days"])
    assert revisions_are_up(frame) is True


def test_finalize_appends_hold_outside_the_top_cut():
    rows = []
    for i in range(25):
        rows.append(
            {
                "ticker": f"T{i}",
                "name": "",
                "sector": "XLK",
                "score": 1 - i * 0.01,
                "signals": ["trend"],
                "return_3m": 0.1,
                "trend_ok": True,
                "book_new_sector": False,
                "hold_recheck": False,
                "headlines": [],
            }
        )
    rows.append(
        {
            "ticker": "OLD",
            "name": "",
            "sector": "XLE",
            "score": 0.01,
            "signals": ["trend", "book"],
            "return_3m": 0.02,
            "trend_ok": True,
            "book_new_sector": False,
            "hold_recheck": True,
            "headlines": [],
        }
    )
    chosen = finalize_candidates(rows, limit=20)
    assert chosen[-1]["ticker"] == "OLD"
    assert len(chosen) == 21


def test_loss_beats_trend_and_horizon():
    now = datetime(2026, 11, 14, tzinfo=UTC)
    trigger, nearest = classify_exit(
        last_close=90,
        avg_price=100,
        sma50=95,
        review_after=now - timedelta(days=1),
        now=now,
        max_loss_pct=0.08,
    )
    assert trigger == "loss"
    assert nearest == "loss"

    trend, _ = classify_exit(
        last_close=96,
        avg_price=100,
        sma50=98,
        review_after=now + timedelta(days=10),
        now=now,
        max_loss_pct=0.08,
    )
    assert trend == "trend"

    horizon, _ = classify_exit(
        last_close=110,
        avg_price=100,
        sma50=90,
        review_after=now - timedelta(hours=1),
        now=now,
        max_loss_pct=0.08,
    )
    assert horizon == "horizon"

    quiet, nearest_quiet = classify_exit(
        last_close=110,
        avg_price=100,
        sma50=90,
        review_after=now + timedelta(days=20),
        now=now,
        max_loss_pct=0.08,
    )
    assert quiet is None
    assert nearest_quiet in {"loss", "trend", "horizon"}


def test_review_schedule_skips_weekends():
    saturday = datetime(2026, 11, 14, 22, tzinfo=UTC)  # Saturday
    assert review_window_open(saturday, 21) is False
    nxt = next_review_at(saturday, 21)
    assert nxt.weekday() == 0
    assert nxt.hour == 21
    wednesday_morning = datetime(2026, 11, 11, 15, tzinfo=UTC)
    assert review_window_open(wednesday_morning, 21) is False
    assert next_review_at(wednesday_morning, 21).hour == 21
    assert next_review_at(wednesday_morning, 21).day == 11


def test_parse_holdings_maps_sectors_and_refuses_a_short_file():
    body = """Fund Holdings as of,"Nov 12, 2026"
Ticker,Name,Sector,Asset Class,Market Value
"BRK B","BERKSHIRE","Financials","Equity","1"
"AAPL","APPLE","Information Technology","Equity","2"
"CASH","USD","Cash and/or Derivatives","Cash","3"
"""
    rows = parse_holdings_csv(body, min_equities=2)
    assert rows[0]["ticker"] == "AAPL"
    assert rows[0]["sector"] == "XLK"
    assert rows[1]["ticker"] == "BRK-B"
    assert rows[1]["sector"] == "XLF"
    try:
        parse_holdings_csv(body, min_equities=90)
        raise AssertionError("expected short file to be refused")
    except ValueError as exc:
        assert "refusing" in str(exc)


def test_sell_requires_a_long_and_flattens_it():
    book = BookSnapshot(
        account_id="DU123",
        cash=100_000,
        positions=[PositionView(ticker="AAPL", quantity=15, avg_cost=100, market_value=1500)],
    )
    thesis = PmThesis(
        ticker="AAPL",
        side=Side.SELL,
        conviction="HIGH",
        size_pct_nav=0.01,
        thesis="Flatten",
    )
    risk = RiskNarrative(ticker="AAPL", narrative="ok", recommend_proceed=True)
    proposal = build_trade_proposal(
        run_id="r1",
        thesis=thesis,
        risk=risk,
        book=book,
        reference_price=100.0,
        model_ids={},
        citation_ids=[],
    )
    assert proposal.quantity == 15

    empty = BookSnapshot(account_id="DU123", cash=100_000, positions=[])
    try:
        build_trade_proposal(
            run_id="r1",
            thesis=thesis,
            risk=risk,
            book=empty,
            reference_price=100.0,
            model_ids={},
            citation_ids=[],
        )
        raise AssertionError("expected rejection")
    except PolicyRejection as exc:
        assert "long" in str(exc).lower()


def test_exit_thesis_is_a_sell_and_skips_the_committee():
    state = {
        "ticker": "AAPL",
        "exit_reason": "below SMA50",
        "nodes": {},
    }
    update = exit_thesis_node(state)
    assert update["pm_thesis"]["side"] == "SELL"
    assert update["risk_narrative"]["recommend_proceed"] is True
    assert update["nodes"]["fundamental"]["status"] == "skipped"
    assert route_after_begin({"run_kind": "exit_rule"}) == "exit_thesis"
    assert route_after_begin({"run_kind": "research"}) == "fanout"


def test_exit_path_reaches_the_inbox(monkeypatch):
    class FakeBroker:
        def get_book(self, market="us"):
            return BookSnapshot(
                account_id="DU123",
                cash=100_000,
                positions=[
                    PositionView(ticker="AAPL", quantity=5, avg_cost=100, market_value=500)
                ],
            )

        def get_quote(self, ticker, market="us", allow_stale=False):
            return 100.0

    monkeypatch.setattr("trader.graph.get_broker", lambda: FakeBroker())
    monkeypatch.setattr(
        "trader.graph.list_open_proposal_tickers_sync", lambda market="us": set()
    )
    graph = build_graph()
    state = initial_state("run-exit", "AAPL", run_kind="exit_rule", exit_reason="below SMA50")
    _result, snapshot, _interrupt = invoke_graph_locked(
        graph, state, {"configurable": {"thread_id": "run-exit"}}
    )
    assert snapshot.next
    proposal = snapshot.values["trade_proposal"]
    assert proposal["side"] == "SELL"
    assert proposal["quantity"] == 5
    assert snapshot.values["nodes"]["fundamental"]["status"] == "skipped"


def test_horizon_clamp_and_average_entry():
    assert clamp_horizon_days(1) == 14
    assert clamp_horizon_days(90) == 60
    assert clamp_horizon_days(None) == 30
    qty, avg = average_entry(10, 100, 10, 120)
    assert qty == 20
    assert avg == 110
