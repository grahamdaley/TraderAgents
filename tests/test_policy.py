from datetime import timedelta

import pytest

from trader.policy import PolicyRejection, build_trade_proposal, revalidate_for_execution
from trader.schemas import (
    BookSnapshot,
    PmThesis,
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
