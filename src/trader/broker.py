from __future__ import annotations

import logging
import threading
from typing import Any

from trader.config import Settings, get_settings
from trader.policy import PolicyRejection, revalidate_for_execution
from trader.schemas import (
    BookSnapshot,
    ExecutionIntent,
    FillRecord,
    PositionView,
    Side,
    TradeProposal,
    utcnow,
)

logger = logging.getLogger(__name__)


class BrokerError(RuntimeError):
    pass


class LiveTradingDisabled(BrokerError):
    pass


class PaperAccountRequired(BrokerError):
    pass


class IBKRAdapter:
    """IBKR order adapter. Paper by default; live only behind an explicit flag."""

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._ib = None
        self._lock = threading.Lock()
        self._mock_cash = self.settings.starting_cash
        self._mock_positions: dict[str, float] = {}
        self._mock_orders: list[dict[str, Any]] = []

    @property
    def using_mock(self) -> bool:
        return self._ib is None

    def assert_trading_mode_safe(self) -> None:
        mode = self.settings.ibkr_trading_mode
        account = self.settings.ibkr_account_id.strip()
        if mode == "paper":
            if account and not account.upper().startswith("DU"):
                raise PaperAccountRequired(
                    f"Paper mode requires a DU* account id; got {account!r}"
                )
            return
        if mode == "live":
            if not self.settings.ibkr_live_orders_enabled:
                raise LiveTradingDisabled(
                    "IBKR_TRADING_MODE=live requires IBKR_LIVE_ORDERS_ENABLED=true"
                )
            if account.upper().startswith("DU"):
                raise BrokerError("Live mode refused for DU paper account id")
            return
        raise BrokerError(f"Unknown trading mode: {mode}")

    def connect(self) -> None:
        self.assert_trading_mode_safe()
        if self.settings.allow_mock_broker and not self.settings.tws_userid:
            logger.warning("IBKR credentials missing; using mock broker book")
            return
        try:
            from ib_async import IB

            ib = IB()
            ib.connect(
                self.settings.ibkr_host,
                self.settings.ibkr_port,
                clientId=self.settings.ibkr_client_id,
                readonly=False,
                timeout=8,
            )
            self._ib = ib
            logger.info(
                "Connected to IBKR Gateway %s:%s mode=%s",
                self.settings.ibkr_host,
                self.settings.ibkr_port,
                self.settings.ibkr_trading_mode,
            )
        except Exception as exc:  # noqa: BLE001
            if self.settings.allow_mock_broker:
                logger.warning("IBKR connect failed (%s); using mock broker", exc)
                self._ib = None
            else:
                raise BrokerError(f"IBKR connect failed: {exc}") from exc

    def disconnect(self) -> None:
        if self._ib is not None:
            try:
                self._ib.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._ib = None

    def get_book(self) -> BookSnapshot:
        self.assert_trading_mode_safe()
        if self._ib is None:
            positions = [
                PositionView(ticker=t, quantity=q, avg_cost=None, market_value=None)
                for t, q in self._mock_positions.items()
                if q
            ]
            return BookSnapshot(
                account_id=self.settings.ibkr_account_id or "MOCK-DU",
                cash=self._mock_cash,
                buying_power=self._mock_cash,
                positions=positions,
                open_orders=list(self._mock_orders),
                as_of=utcnow(),
                source="mock",
            )

        ib = self._ib
        account = self.settings.ibkr_account_id
        cash = 0.0
        buying_power = None
        for av in ib.accountValues(account):
            if av.tag == "TotalCashValue" and av.currency == "USD":
                cash = float(av.value)
            if av.tag == "BuyingPower" and av.currency == "USD":
                buying_power = float(av.value)

        positions: list[PositionView] = []
        for pos in ib.positions(account):
            symbol = pos.contract.symbol
            positions.append(
                PositionView(
                    ticker=symbol,
                    quantity=float(pos.position),
                    avg_cost=float(pos.avgCost) if pos.avgCost else None,
                    market_value=None,
                )
            )

        open_orders = []
        for trade in ib.openTrades():
            open_orders.append(
                {
                    "order_id": trade.order.orderId,
                    "ticker": trade.contract.symbol,
                    "side": trade.order.action,
                    "quantity": trade.order.totalQuantity,
                    "status": trade.orderStatus.status,
                }
            )

        return BookSnapshot(
            account_id=account or "unknown",
            cash=cash,
            buying_power=buying_power,
            positions=positions,
            open_orders=open_orders,
            as_of=utcnow(),
            source="ibkr",
        )

    def get_quote(self, ticker: str) -> float:
        self.assert_trading_mode_safe()
        if self._ib is None:
            # Fall back to last Yahoo close for mock execution checks.
            from trader.tools.market_data import get_market_data_tools

            indicators = get_market_data_tools().compute_indicators(ticker)
            last = (indicators.get("indicators") or {}).get("last_close")
            if not last:
                raise BrokerError(f"No mock quote for {ticker}")
            return float(last)

        from ib_async import Stock

        contract = Stock(ticker.upper(), "SMART", "USD")
        self._ib.qualifyContracts(contract)
        tickers = self._ib.reqTickers(contract)
        if not tickers:
            raise BrokerError(f"No IBKR quote for {ticker}")
        t = tickers[0]
        for candidate in (t.marketPrice(), t.last, t.close):
            if candidate and candidate == candidate and candidate > 0:
                return float(candidate)
        raise BrokerError(f"IBKR quote unavailable for {ticker}")

    def submit(
        self,
        *,
        proposal: TradeProposal,
        intent: ExecutionIntent,
        content_hash: str,
    ) -> FillRecord:
        with self._lock:
            self.assert_trading_mode_safe()
            book = self.get_book()
            live_price = self.get_quote(proposal.ticker)
            revalidate_for_execution(
                proposal=proposal,
                content_hash=content_hash,
                book=book,
                live_price=live_price,
                settings=self.settings,
            )

            if self._ib is None:
                return self._mock_fill(proposal, intent, live_price)

            from ib_async import LimitOrder, MarketOrder, Stock

            contract = Stock(proposal.ticker.upper(), "SMART", "USD")
            self._ib.qualifyContracts(contract)
            action = "BUY" if proposal.side == Side.BUY else "SELL"
            if proposal.order_type.value == "LIMIT" and proposal.limit_price:
                order = LimitOrder(action, proposal.quantity, proposal.limit_price)
            else:
                order = MarketOrder(action, proposal.quantity)
            order.orderRef = intent.client_order_id
            if self.settings.ibkr_account_id:
                order.account = self.settings.ibkr_account_id

            trade = self._ib.placeOrder(contract, order)
            # Wait briefly for a fill or acknowledgement in paper.
            for _ in range(40):
                self._ib.sleep(0.25)
                if trade.orderStatus.status in {
                    "Filled",
                    "Cancelled",
                    "Inactive",
                    "ApiCancelled",
                }:
                    break

            status = trade.orderStatus.status
            if status != "Filled" and trade.orderStatus.filled <= 0:
                raise BrokerError(f"IBKR order not filled: status={status}")

            fill_price = float(trade.orderStatus.avgFillPrice or live_price)
            filled_qty = float(trade.orderStatus.filled or proposal.quantity)
            return FillRecord(
                intent_id=intent.intent_id,
                broker_order_id=str(trade.order.orderId),
                ticker=proposal.ticker.upper(),
                side=proposal.side,
                quantity=filled_qty,
                fill_price=fill_price,
                account_id=self.settings.ibkr_account_id or book.account_id,
            )

    def _mock_fill(
        self, proposal: TradeProposal, intent: ExecutionIntent, live_price: float
    ) -> FillRecord:
        notional = proposal.quantity * live_price
        if proposal.side == Side.BUY:
            self._mock_cash -= notional
            self._mock_positions[proposal.ticker] = (
                self._mock_positions.get(proposal.ticker, 0.0) + proposal.quantity
            )
        else:
            self._mock_cash += notional
            self._mock_positions[proposal.ticker] = max(
                0.0,
                self._mock_positions.get(proposal.ticker, 0.0) - proposal.quantity,
            )
        order_id = f"MOCK-{intent.client_order_id}"
        self._mock_orders.append(
            {
                "order_id": order_id,
                "ticker": proposal.ticker,
                "side": proposal.side.value,
                "quantity": proposal.quantity,
                "status": "Filled",
            }
        )
        return FillRecord(
            intent_id=intent.intent_id,
            broker_order_id=order_id,
            ticker=proposal.ticker.upper(),
            side=proposal.side,
            quantity=proposal.quantity,
            fill_price=live_price,
            account_id=self.settings.ibkr_account_id or "MOCK-DU",
        )


_adapter: IBKRAdapter | None = None


def get_broker() -> IBKRAdapter:
    global _adapter
    if _adapter is None:
        _adapter = IBKRAdapter()
        _adapter.connect()
    return _adapter
