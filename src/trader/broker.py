from __future__ import annotations

import asyncio
import logging
import math
import threading
from collections.abc import Coroutine
from typing import Any, TypeVar

from trader.config import Settings, get_settings
from trader.market_hours import us_equity_session_open
from trader.policy import revalidate_for_execution
from trader.schemas import (
    BookSnapshot,
    ExecutionIntent,
    FillRecord,
    PositionView,
    Side,
    SubmitResult,
    TradeProposal,
    utcnow,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")


class BrokerError(RuntimeError):
    pass


class LiveTradingDisabled(BrokerError):
    pass


class PaperAccountRequired(BrokerError):
    pass


class IBKRAdapter:
    """IBKR order adapter. Paper by default; live only behind an explicit flag.

    IB runs on a dedicated thread/event loop so connect works under uvicorn
    (whose loop is already running). Calls are marshalled via
    ``asyncio.run_coroutine_threadsafe`` — no nest_asyncio.
    """

    def __init__(self, settings: Settings | None = None):
        self.settings = settings or get_settings()
        self._ib = None
        self._lock = threading.Lock()
        self._mock_cash = self.settings.starting_cash
        self._mock_positions: dict[str, float] = {}
        self._mock_orders: list[dict[str, Any]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ib_thread: threading.Thread | None = None

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
        if self._ib is not None:
            return
        if self.settings.allow_mock_broker and not self.settings.tws_userid:
            logger.warning("IBKR credentials missing; using mock broker book")
            return

        ready = threading.Event()
        errors: list[BaseException] = []

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                from ib_async import IB

                ib = IB()

                async def _connect() -> None:
                    await ib.connectAsync(
                        self.settings.ibkr_host,
                        self.settings.ibkr_port,
                        clientId=self.settings.ibkr_client_id,
                        readonly=False,
                        timeout=8,
                    )

                loop.run_until_complete(_connect())
                self._ib = ib
                logger.info(
                    "Connected to IBKR Gateway %s:%s mode=%s",
                    self.settings.ibkr_host,
                    self.settings.ibkr_port,
                    self.settings.ibkr_trading_mode,
                )
                ready.set()
                loop.run_forever()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
                self._ib = None
                ready.set()
            finally:
                try:
                    if self._ib is not None:
                        self._ib.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self._ib = None
                if not loop.is_closed():
                    loop.close()
                self._loop = None

        self._ib_thread = threading.Thread(
            target=runner, name="ibkr-client", daemon=True
        )
        self._ib_thread.start()
        if not ready.wait(timeout=30):
            self.disconnect()
            raise BrokerError("IBKR connect timed out")
        if errors:
            self.disconnect()
            raise BrokerError(f"IBKR connect failed: {errors[0]}") from errors[0]

    def disconnect(self) -> None:
        loop = self._loop
        thread = self._ib_thread
        if loop is not None and loop.is_running():

            def _shutdown() -> None:
                try:
                    if self._ib is not None:
                        self._ib.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self._ib = None
                loop.stop()

            loop.call_soon_threadsafe(_shutdown)
            if thread is not None:
                thread.join(timeout=10)
        else:
            if self._ib is not None:
                try:
                    self._ib.disconnect()
                except Exception:  # noqa: BLE001
                    pass
                self._ib = None
        self._ib_thread = None
        self._loop = None

    def _run_coro(self, coro: Coroutine[Any, Any, T]) -> T:
        loop = self._loop
        if self._ib is None or loop is None or not loop.is_running():
            # Coroutine was created already; close it to avoid "never awaited".
            coro.close()
            raise BrokerError("IBKR not connected")
        return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=60)

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
        return self._run_coro(self._get_book_async())

    async def _get_book_async(self) -> BookSnapshot:
        ib = self._ib
        assert ib is not None
        account = self.settings.ibkr_account_id
        # Run on the IB loop so we touch IB state from the owning thread.
        pos_rows = await ib.reqPositionsAsync()

        # Account base currency may be HKD (or other). IBKR then emits
        # TotalCashValue/BuyingPower/NetLiquidation in BASE, not USD. Per-currency
        # cash for US equity trading lives on $LEDGER-* tags.
        cash = 0.0
        buying_power = None
        ledger_usd = None
        total_cash_usd = None
        total_cash_base = None
        buying_power_usd = None
        buying_power_base = None
        for av in ib.accountValues(account):
            tag = av.tag or ""
            currency = (av.currency or "").upper()
            try:
                value = float(av.value)
            except (TypeError, ValueError):
                continue
            if tag in {"$LEDGER-CashBalance", "$LEDGER-TotalCashBalance", "CashBalance"}:
                if currency == "USD":
                    ledger_usd = value
            elif tag == "TotalCashValue":
                if currency == "USD":
                    total_cash_usd = value
                elif currency in {"BASE", "HKD"}:
                    # Remember base only as last-resort display; prefer USD ledger.
                    if total_cash_base is None or currency == "BASE":
                        total_cash_base = value
            elif tag == "BuyingPower":
                if currency == "USD":
                    buying_power_usd = value
                elif buying_power_base is None:
                    buying_power_base = value

        # Cash used for US equity policy/execution must be USD, not BASE/HKD NAV.
        if total_cash_usd is not None:
            cash = total_cash_usd
        elif ledger_usd is not None:
            cash = ledger_usd
        else:
            cash = 0.0
            logger.warning(
                "No USD cash row from IBKR (TotalCashValue/ledger); "
                "base TotalCashValue=%s buying_power_base=%s",
                total_cash_base,
                buying_power_base,
            )

        buying_power = buying_power_usd if buying_power_usd is not None else None

        positions: list[PositionView] = []
        for pos in pos_rows:
            if account and getattr(pos, "account", None) not in (None, account):
                continue
            # Skip FX/cash ledger rows that show up as zero-qty "USD" positions.
            sec_type = getattr(pos.contract, "secType", None)
            if sec_type in {"CASH", "CMDTY"}:
                continue
            if float(pos.position) == 0:
                continue
            positions.append(
                PositionView(
                    ticker=pos.contract.symbol,
                    quantity=float(pos.position),
                    avg_cost=float(pos.avgCost) if pos.avgCost else None,
                    market_value=None,
                )
            )

        open_orders = [
            {
                "order_id": trade.order.orderId,
                "ticker": trade.contract.symbol,
                "side": trade.order.action,
                "quantity": trade.order.totalQuantity,
                "status": trade.orderStatus.status,
            }
            for trade in ib.openTrades()
        ]

        return BookSnapshot(
            account_id=account or "unknown",
            cash=cash,
            buying_power=buying_power,
            positions=positions,
            open_orders=open_orders,
            as_of=utcnow(),
            source="ibkr",
        )

    def get_quote(self, ticker: str, *, allow_stale: bool = False) -> float:
        self.assert_trading_mode_safe()
        if self._ib is None:
            return self._yahoo_last_close(ticker)

        try:
            return self._run_coro(self._get_quote_async(ticker))
        except BrokerError:
            if not allow_stale and us_equity_session_open():
                raise
            logger.warning(
                "IBKR quote unavailable for %s; falling back to Yahoo last close",
                ticker,
            )
            return self._yahoo_last_close(ticker)

    @staticmethod
    def _yahoo_last_close(ticker: str) -> float:
        from trader.tools.market_data import get_market_data_tools

        indicators = get_market_data_tools().compute_indicators(ticker)
        last = (indicators.get("indicators") or {}).get("last_close")
        if not last:
            raise BrokerError(f"No fallback quote for {ticker}")
        return float(last)

    async def _get_quote_async(self, ticker: str) -> float:
        from ib_async import Stock

        ib = self._ib
        assert ib is not None
        contract = Stock(ticker.upper(), "SMART", "USD")
        await ib.qualifyContractsAsync(contract)
        tickers = await ib.reqTickersAsync(contract)
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
    ) -> SubmitResult:
        with self._lock:
            self.assert_trading_mode_safe()
            book = self.get_book()
            session_open = us_equity_session_open()
            # Off-hours: IB often has no live print; use Yahoo last close / allow stale.
            live_price = self.get_quote(proposal.ticker, allow_stale=not session_open)
            revalidate_for_execution(
                proposal=proposal,
                content_hash=content_hash,
                book=book,
                live_price=live_price,
                settings=self.settings,
                skip_price_band=not session_open,
            )

            if self._ib is None:
                fill = self._mock_fill(proposal, intent, live_price)
                return SubmitResult(
                    status="filled",
                    broker_order_id=fill.broker_order_id,
                    broker_status="Filled",
                    fill=fill,
                )

            return self._run_coro(
                self._submit_async(
                    proposal, intent, live_price, book, session_open=session_open
                )
            )

    async def _submit_async(
        self,
        proposal: TradeProposal,
        intent: ExecutionIntent,
        live_price: float,
        book: BookSnapshot,
        *,
        session_open: bool,
    ) -> SubmitResult:
        from ib_async import LimitOrder, MarketOrder, Stock

        ib = self._ib
        assert ib is not None
        contract = Stock(proposal.ticker.upper(), "SMART", "USD")
        await ib.qualifyContractsAsync(contract)
        action = "BUY" if proposal.side == Side.BUY else "SELL"
        # IBKR API rejects fractional US equity sizes (error 10243).
        share_qty = int(math.floor(float(proposal.quantity)))
        if share_qty < 1:
            raise BrokerError(
                f"Quantity {proposal.quantity} rounds to zero whole shares; "
                "IBKR API cannot place fractional stock orders"
            )
        is_limit = proposal.order_type.value == "LIMIT" and proposal.limit_price
        if is_limit:
            limit = round(float(proposal.limit_price), 2)
            order = LimitOrder(action, share_qty, limit)
            # DAY rests for the next regular session when placed off-hours.
            order.tif = "DAY"
        else:
            if not session_open:
                raise BrokerError(
                    "Market orders cannot be held outside US regular hours; "
                    "use a limit order or approve after the open"
                )
            order = MarketOrder(action, share_qty)
        order.orderRef = intent.client_order_id
        if self.settings.ibkr_account_id:
            order.account = self.settings.ibkr_account_id

        trade = ib.placeOrder(contract, order)
        for _ in range(40):
            await asyncio.sleep(0.25)
            if trade.orderStatus.status in {
                "Filled",
                "Cancelled",
                "Inactive",
                "ApiCancelled",
                "Submitted",
                "PreSubmitted",
            }:
                break

        status = trade.orderStatus.status
        order_id = str(trade.order.orderId)
        if status in {"Cancelled", "ApiCancelled", "Inactive"}:
            detail = ""
            if trade.log:
                detail = trade.log[-1].message or ""
            raise BrokerError(
                f"IBKR order not accepted: status={status}"
                + (f" ({detail})" if detail else "")
            )
        if status == "Filled" or trade.orderStatus.filled > 0:
            fill_price = float(trade.orderStatus.avgFillPrice or live_price)
            filled_qty = float(trade.orderStatus.filled or proposal.quantity)
            fill = FillRecord(
                intent_id=intent.intent_id,
                broker_order_id=order_id,
                ticker=proposal.ticker.upper(),
                side=proposal.side,
                quantity=filled_qty,
                fill_price=fill_price,
                account_id=self.settings.ibkr_account_id or book.account_id,
            )
            return SubmitResult(
                status="filled",
                broker_order_id=order_id,
                broker_status=status,
                fill=fill,
            )

        resting = {"Submitted", "PreSubmitted", "PendingSubmit", "PendingCancel"}
        if is_limit and status in resting:
            when = "next US regular session" if not session_open else "the market"
            return SubmitResult(
                status="submitted",
                broker_order_id=order_id,
                broker_status=status,
                fill=None,
                message=(
                    f"Limit order resting at IBKR (status={status}); "
                    f"will work when {when} is open"
                ),
            )

        raise BrokerError(f"IBKR order not accepted: status={status}")

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
_adapter_lock = threading.Lock()


def get_broker() -> IBKRAdapter:
    global _adapter
    with _adapter_lock:
        if _adapter is None:
            adapter = IBKRAdapter()
            try:
                adapter.connect()
            except Exception:
                adapter.disconnect()
                raise
            _adapter = adapter
        return _adapter
