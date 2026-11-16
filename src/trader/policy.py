from __future__ import annotations

import math
from datetime import timedelta

from trader.config import Settings, get_settings
from trader.schemas import (
    BookSnapshot,
    PmThesis,
    PolicyCheck,
    RiskNarrative,
    Side,
    TradeProposal,
    utcnow,
)


class PolicyRejection(Exception):
    def __init__(self, reason: str, checks: list[PolicyCheck]):
        super().__init__(reason)
        self.reason = reason
        self.checks = checks


def _position_qty(book: BookSnapshot, ticker: str) -> float:
    for pos in book.positions:
        if pos.ticker.upper() == ticker.upper():
            return float(pos.quantity)
    return 0.0


def build_trade_proposal(
    *,
    run_id: str,
    thesis: PmThesis,
    risk: RiskNarrative,
    book: BookSnapshot,
    reference_price: float,
    model_ids: dict[str, str],
    citation_ids: list[str],
    open_proposal_tickers: set[str] | None = None,
    settings: Settings | None = None,
) -> TradeProposal:
    settings = settings or get_settings()
    checks: list[PolicyCheck] = []
    ticker = thesis.ticker.upper()
    open_proposal_tickers = open_proposal_tickers or set()

    if thesis.side == Side.HOLD:
        checks.append(
            PolicyCheck(
                name="hold_no_order",
                passed=True,
                detail="PM recommended HOLD; no order will be proposed",
            )
        )
        raise PolicyRejection("HOLD — no trade proposal", checks)

    if not risk.recommend_proceed:
        checks.append(
            PolicyCheck(
                name="risk_veto",
                passed=False,
                detail="Risk narrative recommended not proceeding",
            )
        )
        raise PolicyRejection("Blocked by risk narrative", checks)

    restricted = ticker in settings.restricted_set
    checks.append(
        PolicyCheck(
            name="restricted_list",
            passed=not restricted,
            detail="Ticker is restricted" if restricted else "Not on restricted list",
        )
    )
    if restricted:
        raise PolicyRejection(f"{ticker} is restricted", checks)

    if ticker in open_proposal_tickers:
        checks.append(
            PolicyCheck(
                name="duplicate_open_proposal",
                passed=False,
                detail="An open proposal already exists for this ticker",
            )
        )
        raise PolicyRejection("Duplicate open proposal", checks)
    checks.append(
        PolicyCheck(
            name="duplicate_open_proposal",
            passed=True,
            detail="No open proposal for ticker",
        )
    )

    size_pct = min(max(thesis.size_pct_nav, 0.0), settings.max_position_pct)
    clipped = size_pct < thesis.size_pct_nav
    checks.append(
        PolicyCheck(
            name="max_position_pct",
            passed=True,
            detail=(
                f"Clipped size from {thesis.size_pct_nav:.2%} to {size_pct:.2%}"
                if clipped
                else f"Size {size_pct:.2%} within max {settings.max_position_pct:.2%}"
            ),
        )
    )

    nav = book.cash + sum(
        (p.market_value or 0.0) for p in book.positions
    )
    if nav <= 0:
        nav = max(book.cash, settings.starting_cash)

    if reference_price <= 0:
        checks.append(
            PolicyCheck(
                name="reference_price",
                passed=False,
                detail="Invalid reference price",
            )
        )
        raise PolicyRejection("Invalid reference price", checks)

    target_notional = nav * size_pct
    # IBKR API rejects fractional US stock orders (error 10243).
    quantity = float(math.floor(target_notional / reference_price))
    current_qty = _position_qty(book, ticker)

    if thesis.side == Side.BUY:
        notional = quantity * reference_price
        if quantity <= 0 or notional > book.cash:
            checks.append(
                PolicyCheck(
                    name="cash_check",
                    passed=False,
                    detail=(
                        f"Need ~{target_notional:.2f} ({quantity} whole shares), "
                        f"cash={book.cash:.2f}"
                    ),
                )
            )
            raise PolicyRejection("Insufficient cash", checks)
        checks.append(
            PolicyCheck(
                name="cash_check",
                passed=True,
                detail=f"Cash {book.cash:.2f} covers {quantity} shares (~{notional:.2f})",
            )
        )
    elif thesis.side == Side.SELL:
        if current_qty <= 0:
            checks.append(
                PolicyCheck(
                    name="sell_requires_long",
                    passed=False,
                    detail="No long position to flatten",
                )
            )
            raise PolicyRejection("SELL requires an existing long position", checks)
        quantity = float(math.floor(current_qty))
        if nav > 0:
            size_pct = min(1.0, (quantity * reference_price) / nav)
        checks.append(
            PolicyCheck(
                name="sell_requires_long",
                passed=True,
                detail=f"Flattening {quantity} shares",
            )
        )

    if quantity <= 0:
        checks.append(
            PolicyCheck(
                name="quantity_positive",
                passed=False,
                detail="Computed whole-share quantity is zero",
            )
        )
        raise PolicyRejection("Quantity is zero", checks)

    limit_price = round(
        reference_price * (1.001 if thesis.side == Side.BUY else 0.999), 2
    )
    expires_at = utcnow() + timedelta(minutes=settings.proposal_ttl_minutes)
    proposal = TradeProposal(
        run_id=run_id,
        ticker=ticker,
        side=thesis.side,
        quantity=quantity,
        size_pct_nav=size_pct,
        limit_price=limit_price,
        reference_price=reference_price,
        thesis_summary=thesis.thesis,
        citation_ids=citation_ids,
        policy_checks=checks,
        model_ids=model_ids,
        expires_at=expires_at,
    )
    return proposal.with_hash()


def revalidate_for_execution(
    *,
    proposal: TradeProposal,
    content_hash: str,
    book: BookSnapshot,
    live_price: float,
    settings: Settings | None = None,
    skip_price_band: bool = False,
) -> list[PolicyCheck]:
    settings = settings or get_settings()
    checks: list[PolicyCheck] = []

    if proposal.content_hash != content_hash:
        raise PolicyRejection(
            "Content hash mismatch",
            [
                PolicyCheck(
                    name="content_hash",
                    passed=False,
                    detail="Proposal was amended; re-approval required",
                )
            ],
        )
    checks.append(
        PolicyCheck(name="content_hash", passed=True, detail="Hash matches approval")
    )

    if utcnow() > proposal.expires_at:
        raise PolicyRejection(
            "Proposal expired",
            [PolicyCheck(name="ttl", passed=False, detail="Past expires_at")],
        )
    checks.append(PolicyCheck(name="ttl", passed=True, detail="Within TTL"))

    if skip_price_band:
        checks.append(
            PolicyCheck(
                name="price_band",
                passed=True,
                detail="Skipped outside US regular hours; limit order will rest at IBKR",
            )
        )
    else:
        band = settings.price_band_pct
        move = abs(live_price - proposal.reference_price) / proposal.reference_price
        if move > band:
            raise PolicyRejection(
                "Price moved outside band",
                [
                    PolicyCheck(
                        name="price_band",
                        passed=False,
                        detail=(
                            f"Live {live_price:.4f} vs ref {proposal.reference_price:.4f} "
                            f"({move:.2%} > {band:.2%})"
                        ),
                    )
                ],
            )
        checks.append(
            PolicyCheck(
                name="price_band",
                passed=True,
                detail=f"Price move {move:.2%} within {band:.2%}",
            )
        )

    if proposal.ticker.upper() in settings.restricted_set:
        raise PolicyRejection(
            "Ticker restricted",
            [
                PolicyCheck(
                    name="restricted_list",
                    passed=False,
                    detail="Restricted at execution time",
                )
            ],
        )

    if proposal.side == Side.BUY:
        notional = proposal.quantity * live_price
        if notional > book.cash:
            raise PolicyRejection(
                "Insufficient cash at execution",
                [
                    PolicyCheck(
                        name="cash_check",
                        passed=False,
                        detail=f"Need {notional:.2f}, cash={book.cash:.2f}",
                    )
                ],
            )
        checks.append(
            PolicyCheck(
                name="cash_check",
                passed=True,
                detail=f"Cash {book.cash:.2f} covers notional {notional:.2f}",
            )
        )
    elif proposal.side == Side.SELL:
        current_qty = _position_qty(book, proposal.ticker)
        if current_qty <= 0:
            raise PolicyRejection(
                "SELL requires an existing long position",
                [
                    PolicyCheck(
                        name="sell_requires_long",
                        passed=False,
                        detail="No long position at execution",
                    )
                ],
            )
        if proposal.quantity > current_qty:
            raise PolicyRejection(
                "SELL quantity exceeds the live long",
                [
                    PolicyCheck(
                        name="sell_requires_long",
                        passed=False,
                        detail=(
                            f"Requested {proposal.quantity} exceeds long {current_qty}"
                        ),
                    )
                ],
            )
        checks.append(
            PolicyCheck(
                name="sell_requires_long",
                passed=True,
                detail=f"Long {current_qty} covers sell {proposal.quantity}",
            )
        )

    return checks
