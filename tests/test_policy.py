from datetime import timedelta

import pytest

from trader.graph import execute_node
from trader.policy import (
    PolicyRejection,
    build_trade_proposal,
    revalidate_for_execution,
)
from trader.schemas import (
    BookSnapshot,
    PmThesis,
    PositionView,
    RiskNarrative,
    Side,
    TradeProposal,
    utcnow,
)


def test_build_buy_proposal_clips_size():
    thesis = PmThesis(
        ticker="AAPL",
        side=Side.BUY,
        conviction="HIGH",
        size_pct_nav=0.5,
        thesis="Buy the dip",
    )
    risk = RiskNarrative(
        ticker="AAPL", narrative="ok", recommend_proceed=True
    )
    book = BookSnapshot(account_id="DU123", cash=100_000, positions=[])
    proposal = build_trade_proposal(
        run_id="r1",
        thesis=thesis,
        risk=risk,
        book=book,
        reference_price=100.0,
        model_ids={"pm": "mock"},
        citation_ids=["c1"],
    )
    assert proposal.size_pct_nav == pytest.approx(0.10)
    assert proposal.quantity == pytest.approx(100.0)
    assert proposal.content_hash


def test_hold_rejected():
    thesis = PmThesis(
        ticker="AAPL",
        side=Side.HOLD,
        conviction="LOW",
        size_pct_nav=0.0,
        thesis="Wait",
    )
    risk = RiskNarrative(ticker="AAPL", narrative="ok", recommend_proceed=True)
    book = BookSnapshot(account_id="DU123", cash=100_000, positions=[])
    with pytest.raises(PolicyRejection):
        build_trade_proposal(
            run_id="r1",
            thesis=thesis,
            risk=risk,
            book=book,
            reference_price=100.0,
            model_ids={},
            citation_ids=[],
        )


def test_buy_bumps_to_one_board_lot_when_cash_allows():
    """3% of NAV can round below a 1000-share HK lot; bump if cash covers it."""
    thesis = PmThesis(
        ticker="2319.HK",
        side=Side.BUY,
        conviction="MEDIUM",
        size_pct_nav=0.03,
        thesis="Tactical long",
    )
    risk = RiskNarrative(
        ticker="2319.HK", narrative="ok", recommend_proceed=True
    )
    book = BookSnapshot(
        account_id="DU123",
        cash=451_643.66,
        positions=[],
        market="hk",
        currency="HKD",
    )
    proposal = build_trade_proposal(
        run_id="r1",
        thesis=thesis,
        risk=risk,
        book=book,
        reference_price=22.04,
        model_ids={"pm": "mock"},
        citation_ids=["c1"],
        market="hk",
    )
    assert proposal.quantity == pytest.approx(1000.0)
    assert proposal.size_pct_nav == pytest.approx(1000.0 * 22.04 / 451_643.66)
    assert any(c.name == "board_lot_bump" and c.passed for c in proposal.policy_checks)


def test_hk_limit_price_snaps_to_sehk_tick():
    """Regression: 0669.HK limits at *.14/*.64 were rejected (IBKR Error 110)."""
    thesis = PmThesis(
        ticker="0669.HK",
        side=Side.BUY,
        conviction="MEDIUM",
        size_pct_nav=0.03,
        thesis="Starter long",
    )
    risk = RiskNarrative(
        ticker="0669.HK", narrative="ok", recommend_proceed=True
    )
    book = BookSnapshot(
        account_id="DU123",
        cash=500_000.0,
        positions=[],
        market="hk",
        currency="HKD",
    )
    proposal = build_trade_proposal(
        run_id="r1",
        thesis=thesis,
        risk=risk,
        book=book,
        reference_price=137.0,
        model_ids={"pm": "mock"},
        citation_ids=["c1"],
        market="hk",
    )
    # 137.0 * 1.001 = 137.137 → nearest 0.10 tick is 137.10
    assert proposal.limit_price == pytest.approx(137.10)
    assert abs(proposal.limit_price * 10 - round(proposal.limit_price * 10)) < 1e-9


def test_buy_rejects_when_one_board_lot_unaffordable():
    thesis = PmThesis(
        ticker="2319.HK",
        side=Side.BUY,
        conviction="MEDIUM",
        size_pct_nav=0.03,
        thesis="Tactical long",
    )
    risk = RiskNarrative(
        ticker="2319.HK", narrative="ok", recommend_proceed=True
    )
    book = BookSnapshot(
        account_id="DU123",
        cash=10_000.0,
        positions=[],
        market="hk",
        currency="HKD",
    )
    with pytest.raises(PolicyRejection, match="Insufficient cash"):
        build_trade_proposal(
            run_id="r1",
            thesis=thesis,
            risk=risk,
            book=book,
            reference_price=22.04,
            model_ids={},
            citation_ids=[],
            market="hk",
        )


def test_price_band_blocks_execution():
    proposal = TradeProposal(
        run_id="r1",
        ticker="AAPL",
        side=Side.BUY,
        quantity=10,
        size_pct_nav=0.05,
        reference_price=100.0,
        limit_price=100.1,
        thesis_summary="x",
        expires_at=utcnow() + timedelta(minutes=30),
    ).with_hash()
    book = BookSnapshot(account_id="DU123", cash=100_000, positions=[])
    with pytest.raises(PolicyRejection):
        revalidate_for_execution(
            proposal=proposal,
            content_hash=proposal.content_hash,
            book=book,
            live_price=110.0,
        )


def _sell_proposal(quantity: float = 10.0) -> TradeProposal:
    return TradeProposal(
        run_id="r1",
        ticker="AAPL",
        side=Side.SELL,
        quantity=quantity,
        size_pct_nav=0.05,
        reference_price=100.0,
        limit_price=99.9,
        thesis_summary="flatten",
        expires_at=utcnow() + timedelta(minutes=30),
    ).with_hash()


def test_revalidate_sell_requires_long_and_blocks_oversized():
    long_book = BookSnapshot(
        account_id="DU123",
        cash=100_000,
        positions=[PositionView(ticker="AAPL", quantity=5, avg_cost=100, market_value=500)],
    )
    proposal = _sell_proposal(5)
    checks = revalidate_for_execution(
        proposal=proposal,
        content_hash=proposal.content_hash,
        book=long_book,
        live_price=100.0,
    )
    assert any(c.name == "sell_requires_long" and c.passed for c in checks)

    empty = BookSnapshot(account_id="DU123", cash=100_000, positions=[])
    with pytest.raises(PolicyRejection, match="long"):
        revalidate_for_execution(
            proposal=proposal,
            content_hash=proposal.content_hash,
            book=empty,
            live_price=100.0,
        )

    oversized = _sell_proposal(20)
    with pytest.raises(PolicyRejection, match="exceeds"):
        revalidate_for_execution(
            proposal=oversized,
            content_hash=oversized.content_hash,
            book=long_book,
            live_price=100.0,
        )


def test_approve_with_edits_clamps_sell_to_live_long(monkeypatch):
    class FakeBroker:
        def get_book(self, market="us"):
            return BookSnapshot(
                account_id="DU123",
                cash=100_000,
                positions=[
                    PositionView(ticker="AAPL", quantity=5, avg_cost=100, market_value=500)
                ],
            )

        def submit(self, *, proposal, intent, content_hash):
            assert proposal.quantity == 5
            assert intent.quantity == 5
            from trader.schemas import FillRecord, SubmitResult

            fill = FillRecord(
                intent_id=intent.intent_id,
                broker_order_id="MOCK-1",
                ticker="AAPL",
                side=Side.SELL,
                quantity=5,
                fill_price=100.0,
                account_id="DU123",
            )
            return SubmitResult(
                status="filled",
                broker_order_id=fill.broker_order_id,
                broker_status="Filled",
                fill=fill,
            )

    monkeypatch.setattr("trader.graph.get_broker", lambda: FakeBroker())
    proposal = _sell_proposal(5)
    result = execute_node(
        {
            "run_id": "r1",
            "trade_proposal": proposal.model_dump(mode="json"),
            "human_decision": {
                "action": "approve_with_edits",
                "quantity": 50,
                "limit_price": 99.5,
            },
            "nodes": {},
        }
    )
    assert result["status"] == "filled"
    assert result["trade_proposal"]["quantity"] == 5
    assert result["execution_intent"]["quantity"] == 5
