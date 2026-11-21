from __future__ import annotations

import asyncio
import logging
import math
import threading
from collections.abc import Callable, Coroutine
from typing import Any, TypeVar

from trader.config import Settings, get_settings
from trader.market_hours import equity_session_open
from trader.markets import (
    MarketId,
    display_ticker_from_ibkr,
    get_market,
    ibkr_symbol,
    lot_size_for,
    parse_market,
    round_to_lot,
    yahoo_symbol,
)
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
        self._connect_lock = threading.Lock()
        self._intentional_mock = False
        self._mock_cash = self.settings.starting_cash
        self._mock_positions: dict[str, float] = {}
        self._mock_orders: list[dict[str, Any]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._ib_thread: threading.Thread | None = None

    @property
    def using_mock(self) -> bool:
        return self._intentional_mock

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

    def _is_connected(self) -> bool:
        ib = self._ib
        loop = self._loop
        if ib is None or loop is None or not loop.is_running():
            return False
        try:
            return bool(ib.isConnected())
        except Exception:  # noqa: BLE001
            return False

    @staticmethod
    def _is_disconnect_error(exc: BaseException) -> bool:
        text = str(exc).lower()
        needles = (
            "not connected",
            "disconnected",
            "peer closed",
            "connection reset",
            "broken pipe",
            "timeout",
            "client id is already in use",
            "already in use",
        )
        return any(n in text for n in needles)

    def connect(self) -> None:
        self.assert_trading_mode_safe()
        if self._is_connected():
            return
        if self.settings.allow_mock_broker and not self.settings.tws_userid:
            logger.warning("IBKR credentials missing; using mock broker book")
            self._intentional_mock = True
            return

        self._intentional_mock = False
        with self._connect_lock:
            if self._is_connected():
                return
            if self._loop is not None and self._loop.is_running() and self._ib is not None:
                # Thread still alive — reconnect on the existing loop.
                self._reconnect_on_loop(retries=3)
                return
            self._start_ib_thread()

    def _start_ib_thread(self) -> None:
        # Ensure any prior half-dead thread is gone before starting another.
        self._shutdown_ib_thread()
        ready = threading.Event()
        errors: list[BaseException] = []

        def runner() -> None:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._loop = loop
            try:
                loop.run_until_complete(self._connect_ib_async())
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
        if not self._is_connected():
            self.disconnect()
            raise BrokerError("IBKR connect finished without a live session")

    async def _connect_ib_async(self) -> None:
        from ib_async import IB

        # Drop any stale client before opening a new socket.
        old = self._ib
        if old is not None:
            try:
                if old.isConnected():
                    old.disconnect()
            except Exception:  # noqa: BLE001
                pass
            self._ib = None

        ib = IB()

        def _on_disconnected() -> None:
            logger.warning(
                "IBKR Gateway disconnected (host=%s port=%s); will reconnect on next use",
                self.settings.ibkr_host,
                self.settings.ibkr_port,
            )

        ib.disconnectedEvent += _on_disconnected
        await ib.connectAsync(
            self.settings.ibkr_host,
            self.settings.ibkr_port,
            clientId=self.settings.ibkr_client_id,
            readonly=False,
            timeout=8,
        )
        self._ib = ib
        logger.info(
            "Connected to IBKR Gateway %s:%s mode=%s clientId=%s",
            self.settings.ibkr_host,
            self.settings.ibkr_port,
            self.settings.ibkr_trading_mode,
            self.settings.ibkr_client_id,
        )

    def _reconnect_on_loop(self, *, retries: int = 3) -> None:
        loop = self._loop
        if loop is None or not loop.is_running():
            self._start_ib_thread()
            return
        last_exc: BaseException | None = None
        for attempt in range(1, retries + 1):
            try:
                asyncio.run_coroutine_threadsafe(
                    self._connect_ib_async(), loop
                ).result(timeout=30)
                if self._is_connected():
                    return
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                logger.warning(
                    "IBKR reconnect attempt %s/%s failed: %s",
                    attempt,
                    retries,
                    exc,
                )
                threading.Event().wait(min(2 * attempt, 6))
        # Loop may be wedged — full thread restart.
        logger.warning("IBKR reconnect via loop failed; restarting IB thread")
        self._shutdown_ib_thread()
        try:
            self._start_ib_thread()
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(
                f"IBKR reconnect failed: {last_exc or exc}"
            ) from (last_exc or exc)

    def ensure_connected(self) -> None:
        """Reconnect after Gateway restarts / nightly re-auth without API reboot."""
        if self._intentional_mock:
            return
        if self._is_connected():
            return
        with self._connect_lock:
            if self._is_connected():
                return
            if self._intentional_mock:
                return
            logger.warning("IBKR session not connected; reconnecting to Gateway")
            if self._loop is not None and self._loop.is_running():
                self._reconnect_on_loop(retries=3)
            else:
                self._start_ib_thread()
            if not self._is_connected():
                raise BrokerError("IBKR not connected")

    def _shutdown_ib_thread(self) -> None:
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

    def disconnect(self) -> None:
        with self._connect_lock:
            self._shutdown_ib_thread()

    def _run_coro(
        self, coro: Coroutine[Any, Any, T], *, timeout: float = 60
    ) -> T:
        loop = self._loop
        if not self._is_connected() or loop is None or not loop.is_running():
            # Coroutine was created already; close it to avoid "never awaited".
            coro.close()
            raise BrokerError("IBKR not connected")
        try:
            return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)
        except TimeoutError as exc:
            raise BrokerError(
                f"IBKR request timed out after {timeout:.0f}s"
            ) from exc

    def _with_ib(self, factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
        """Run an IB coroutine factory, reconnecting once on disconnect errors."""
        self.ensure_connected()
        try:
            return self._run_coro(factory())
        except Exception as exc:  # noqa: BLE001
            if self._intentional_mock or not self._is_disconnect_error(exc):
                raise
            logger.warning(
                "IBKR call failed after disconnect (%s); retrying once", exc
            )
            with self._connect_lock:
                if not self._is_connected():
                    self._reconnect_on_loop(retries=3)
            return self._run_coro(factory())

    def _mock_book(self, market: MarketId = "us") -> BookSnapshot:
        spec = get_market(market)
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
            market=spec.id,
            currency=spec.currency,
        )

    @staticmethod
    def _make_stock(ticker: str, market: MarketId | str):
        from ib_async import Stock

        mid = parse_market(str(market))
        spec = get_market(mid)
        return Stock(ibkr_symbol(ticker, mid), spec.ib_exchange, spec.currency)

    def get_book(self, market: MarketId | str = "us") -> BookSnapshot:
        mid = parse_market(str(market))
        self.assert_trading_mode_safe()
        if self._intentional_mock:
            return self._mock_book(mid)
        return self._with_ib(lambda: self._get_book_async(mid))

    async def _get_book_async(self, market: MarketId = "us") -> BookSnapshot:
        ib = self._ib
        assert ib is not None
        account = self.settings.ibkr_account_id
        spec = get_market(market)
        target_ccy = spec.currency
        # Run on the IB loop so we touch IB state from the owning thread.
        pos_rows = await ib.reqPositionsAsync()

        # Account base currency may differ from the desk currency. Prefer
        # TotalCashValue / ledger rows tagged with the desk currency.
        cash = 0.0
        buying_power = None
        ledger_ccy = None
        total_cash_ccy = None
        total_cash_base = None
        buying_power_ccy = None
        buying_power_base = None
        for av in ib.accountValues(account):
            tag = av.tag or ""
            currency = (av.currency or "").upper()
            try:
                value = float(av.value)
            except (TypeError, ValueError):
                continue
            if tag in {"$LEDGER-CashBalance", "$LEDGER-TotalCashBalance", "CashBalance"}:
                if currency == target_ccy:
                    ledger_ccy = value
            elif tag == "TotalCashValue":
                if currency == target_ccy:
                    total_cash_ccy = value
                elif currency == "BASE" or (market == "us" and currency == "HKD"):
                    if total_cash_base is None or currency == "BASE":
                        total_cash_base = value
            elif tag == "BuyingPower":
                if currency == target_ccy:
                    buying_power_ccy = value
                elif buying_power_base is None:
                    buying_power_base = value

        if total_cash_ccy is not None:
            cash = total_cash_ccy
        elif ledger_ccy is not None:
            cash = ledger_ccy
        else:
            cash = 0.0
            logger.warning(
                "No %s cash row from IBKR (TotalCashValue/ledger); "
                "base TotalCashValue=%s buying_power_base=%s",
                target_ccy,
                total_cash_base,
                buying_power_base,
            )

        buying_power = buying_power_ccy if buying_power_ccy is not None else None

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
            pos_ccy = (getattr(pos.contract, "currency", None) or "").upper()
            exchange = (
                getattr(pos.contract, "primaryExchange", None)
                or getattr(pos.contract, "exchange", None)
                or ""
            )
            exchange_u = str(exchange).upper()
            if market == "us":
                if pos_ccy and pos_ccy not in {"USD", ""}:
                    continue
                if exchange_u in {"SEHK", "HKFE"}:
                    continue
            else:
                if pos_ccy and pos_ccy not in {"HKD", ""}:
                    continue
                if pos_ccy != "HKD" and exchange_u and exchange_u not in {"SEHK", "HKEX"}:
                    # Without currency, keep only SEHK-looking contracts.
                    if not str(pos.contract.symbol or "").isdigit():
                        continue
            display = display_ticker_from_ibkr(
                str(pos.contract.symbol or ""), market, pos_ccy or None
            )
            positions.append(
                PositionView(
                    ticker=display,
                    quantity=float(pos.position),
                    avg_cost=float(pos.avgCost) if pos.avgCost else None,
                    market_value=None,
                    currency=pos_ccy or target_ccy,
                    exchange=exchange_u or spec.ib_exchange,
                )
            )

        try:
            await ib.reqAllOpenOrdersAsync()
        except Exception:  # noqa: BLE001
            logger.exception("reqAllOpenOrders failed while building book")

        open_orders = []
        for trade in ib.openTrades():
            order_ccy = (getattr(trade.contract, "currency", None) or "").upper()
            if market == "us" and order_ccy and order_ccy not in {"USD", ""}:
                continue
            if market == "hk" and order_ccy and order_ccy not in {"HKD", ""}:
                continue
            order_type = str(trade.order.orderType or "")
            limit_price = None
            if order_type == "LMT" and trade.order.lmtPrice is not None:
                limit_price = float(trade.order.lmtPrice)
            open_orders.append(
                {
                    "order_id": trade.order.orderId,
                    "ticker": display_ticker_from_ibkr(
                        str(trade.contract.symbol or ""), market, order_ccy or None
                    ),
                    "side": trade.order.action,
                    "quantity": float(trade.order.totalQuantity),
                    "order_type": order_type,
                    "limit_price": limit_price,
                    "tif": str(trade.order.tif or ""),
                    "status": trade.orderStatus.status,
                    "filled": float(trade.orderStatus.filled or 0),
                    "remaining": float(trade.orderStatus.remaining or 0),
                    "order_ref": str(getattr(trade.order, "orderRef", "") or ""),
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
            market=spec.id,
            currency=target_ccy,
        )

    def get_quote(
        self,
        ticker: str,
        *,
        market: MarketId | str = "us",
        allow_stale: bool = False,
    ) -> float:
        mid = parse_market(str(market))
        yahoo = yahoo_symbol(ticker, mid)
        self.assert_trading_mode_safe()
        if self._intentional_mock:
            return self._yahoo_last_close(yahoo)

        try:
            return self._with_ib(lambda: self._get_quote_async(yahoo, mid))
        except BrokerError:
            # Paper accounts often lack live API MD (error 10089). Prefer delayed
            # IBKR above; if that still fails, Yahoo keeps research/execution unblocked.
            logger.warning(
                "IBKR quote unavailable for %s; falling back to Yahoo last close",
                yahoo,
            )
            return self._yahoo_last_close(yahoo)

    @staticmethod
    def _yahoo_last_close(ticker: str) -> float:
        from trader.tools.market_data import get_market_data_tools

        indicators = get_market_data_tools().compute_indicators(ticker)
        last = (indicators.get("indicators") or {}).get("last_close")
        if not last:
            raise BrokerError(f"No fallback quote for {ticker}")
        return float(last)
    @staticmethod
    def _price_from_ticker(t: Any) -> float | None:
        for candidate in (t.marketPrice(), t.last, t.close, getattr(t, "bid", None), getattr(t, "ask", None)):
            if candidate and candidate == candidate and float(candidate) > 0:
                return float(candidate)
        return None

    async def _get_quote_async(self, ticker: str, market: MarketId = "us") -> float:
        ib = self._ib
        assert ib is not None
        contract = self._make_stock(ticker, market)
        await ib.qualifyContractsAsync(contract)

        # 1=live, 3=delayed, 4=delayed-frozen. Live API MD often needs a paid
        # subscription (10089); delayed is available on paper without it.
        last_error = "no quote"
        for md_type in (1, 3, 4):
            try:
                ib.reqMarketDataType(md_type)
            except Exception:  # noqa: BLE001
                logger.debug("reqMarketDataType(%s) failed", md_type, exc_info=True)
            tickers = await ib.reqTickersAsync(contract)
            if tickers:
                price = self._price_from_ticker(tickers[0])
                if price is not None:
                    return price
            # Snapshot stream as a second chance for delayed farms.
            ticker_row = ib.reqMktData(contract, "", False, False)
            await asyncio.sleep(1.25)
            price = self._price_from_ticker(ticker_row)
            ib.cancelMktData(contract)
            if price is not None:
                return price
            last_error = f"md_type={md_type} empty"
        raise BrokerError(f"IBKR quote unavailable for {ticker} ({last_error})")

    def submit(
        self,
        *,
        proposal: TradeProposal,
        intent: ExecutionIntent,
        content_hash: str,
    ) -> SubmitResult:
        with self._lock:
            mid = parse_market(getattr(proposal, "market", None) or "us")
            self.assert_trading_mode_safe()
            book = self.get_book(mid)
            session_open = equity_session_open(mid)
            # Off-hours: IB often has no live print; use Yahoo last close / allow stale.
            live_price = self.get_quote(
                proposal.ticker, market=mid, allow_stale=not session_open
            )
            revalidate_for_execution(
                proposal=proposal,
                content_hash=content_hash,
                book=book,
                live_price=live_price,
                settings=self.settings,
                skip_price_band=not session_open,
            )

            if self._intentional_mock:
                fill = self._mock_fill(proposal, intent, live_price)
                return SubmitResult(
                    status="filled",
                    broker_order_id=fill.broker_order_id,
                    broker_status="Filled",
                    fill=fill,
                )

            return self._with_ib(
                lambda: self._submit_async(
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
        from ib_async import LimitOrder, MarketOrder

        ib = self._ib
        assert ib is not None
        mid = parse_market(getattr(proposal, "market", None) or "us")
        spec = get_market(mid)
        contract = self._make_stock(proposal.ticker, mid)
        await ib.qualifyContractsAsync(contract)
        action = "BUY" if proposal.side == Side.BUY else "SELL"
        lot = lot_size_for(proposal.ticker, mid)
        # Prefer IBKR minSize when the contract exposes it.
        min_size = getattr(contract, "minSize", None)
        try:
            if min_size is not None and float(min_size) > 0:
                lot = max(int(float(min_size)), 1)
        except (TypeError, ValueError):
            pass
        share_qty = int(round_to_lot(float(proposal.quantity), lot))
        if share_qty < 1:
            raise BrokerError(
                f"Quantity {proposal.quantity} rounds to zero lot shares "
                f"(lot={lot}); cannot place order"
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
                    f"Market orders cannot be held outside {spec.label} regular hours; "
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
        order_id = str(trade.order.orderId or getattr(trade.orderStatus, "orderId", "") or "")
        if order_id in {"0", "None"}:
            order_id = ""
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
                market=mid,
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
            when = (
                f"next {spec.label} regular session" if not session_open else "the market"
            )
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

    def lookup_order_outcomes(
        self, queries: list[dict[str, str]]
    ) -> dict[str, dict[str, Any]]:
        """Resolve resting intents against IBKR open/completed orders.

        ``queries`` items use ``client_order_id`` (orderRef) and optional
        ``broker_order_id``. Returns a map keyed by client_order_id.
        """
        if not queries:
            return {}
        self.assert_trading_mode_safe()
        if self._intentional_mock:
            return {}
        # Executions + open/completed orders can exceed the default 60s budget.
        self.ensure_connected()
        try:
            return self._run_coro(
                self._lookup_order_outcomes_async(queries), timeout=120
            )
        except Exception as exc:  # noqa: BLE001
            if self._intentional_mock or not self._is_disconnect_error(exc):
                raise
            logger.warning(
                "IBKR lookup failed after disconnect (%s); retrying once", exc
            )
            with self._connect_lock:
                if not self._is_connected():
                    self._reconnect_on_loop(retries=3)
            return self._run_coro(
                self._lookup_order_outcomes_async(queries), timeout=120
            )

    async def _lookup_order_outcomes_async(
        self, queries: list[dict[str, str]]
    ) -> dict[str, dict[str, Any]]:
        ib = self._ib
        assert ib is not None

        # Executions are the source of truth for late fills; fetch them first.
        exec_by_ref: dict[str, dict[str, Any]] = {}
        exec_by_oid: dict[str, dict[str, Any]] = {}
        try:
            from ib_async import ExecutionFilter

            fill_rows = await asyncio.wait_for(
                ib.reqExecutionsAsync(ExecutionFilter()), timeout=20
            )
            for fill in fill_rows:
                execution = fill.execution
                ref = str(getattr(execution, "orderRef", None) or "").strip()
                oid = str(execution.orderId or "")
                if oid in {"0", "None"}:
                    oid = ""
                shares = float(execution.shares or 0)
                price = float(
                    getattr(execution, "avgPrice", None) or execution.price or 0
                )
                if shares <= 0 or price <= 0:
                    continue
                side = str(execution.side or "").upper()
                if side in {"BOT", "BUY"}:
                    side = "BUY"
                elif side in {"SLD", "SELL"}:
                    side = "SELL"
                entry = {
                    "broker_order_id": oid,
                    "client_order_id": ref,
                    "status": "Filled",
                    "filled_qty": shares,
                    "avg_price": price,
                    "remaining": 0.0,
                    "ticker": getattr(fill.contract, "symbol", "") or "",
                    "side": side,
                }

                def _accumulate(bucket: dict[str, dict[str, Any]], key: str) -> None:
                    prev = bucket.get(key)
                    if not prev:
                        bucket[key] = dict(entry)
                        return
                    total_shares = prev["filled_qty"] + shares
                    prev["avg_price"] = (
                        prev["avg_price"] * prev["filled_qty"] + price * shares
                    ) / total_shares
                    prev["filled_qty"] = total_shares
                    if oid:
                        prev["broker_order_id"] = oid
                    if ref:
                        prev["client_order_id"] = ref

                if ref:
                    _accumulate(exec_by_ref, ref)
                if oid:
                    _accumulate(exec_by_oid, oid)
        except Exception:  # noqa: BLE001
            logger.exception("reqExecutions failed during fill reconcile")

        # Open/completed order status is helpful but must not hang reconcile.
        try:
            await asyncio.wait_for(ib.reqAllOpenOrdersAsync(), timeout=10)
        except Exception:  # noqa: BLE001
            logger.warning("reqAllOpenOrders timed out/failed during fill reconcile")
        try:
            await asyncio.wait_for(ib.reqCompletedOrdersAsync(True), timeout=10)
        except Exception:  # noqa: BLE001
            logger.warning("reqCompletedOrders timed out/failed during fill reconcile")

        by_ref: dict[str, dict[str, Any]] = dict(exec_by_ref)
        by_id: dict[str, dict[str, Any]] = dict(exec_by_oid)
        for v in exec_by_ref.values():
            oid = str(v.get("broker_order_id") or "")
            if oid and oid not in by_id:
                by_id[oid] = v
        for trade in ib.trades():
            order = trade.order
            status = trade.orderStatus
            ref = str(getattr(order, "orderRef", None) or "").strip()
            oid = str(order.orderId or "")
            if oid in {"0", "None"}:
                oid = ""
            filled_qty = float(status.filled or 0)
            avg_price = float(status.avgFillPrice or 0)
            if ref and ref in exec_by_ref:
                # Prefer execution totals when completed-order status is empty.
                filled_qty = max(filled_qty, float(exec_by_ref[ref]["filled_qty"]))
                avg_price = avg_price or float(exec_by_ref[ref]["avg_price"])
                if not oid:
                    oid = str(exec_by_ref[ref].get("broker_order_id") or "")
            elif filled_qty > 0 and avg_price <= 0 and trade.fills:
                notionals = [
                    float(f.execution.shares) * float(f.execution.price)
                    for f in trade.fills
                    if float(f.execution.shares or 0) > 0
                ]
                shares = sum(float(f.execution.shares or 0) for f in trade.fills)
                if shares > 0 and notionals:
                    avg_price = sum(notionals) / shares
            entry = {
                "broker_order_id": oid,
                "client_order_id": ref,
                "status": str(status.status or ""),
                "filled_qty": filled_qty,
                "avg_price": avg_price,
                "remaining": float(status.remaining or 0),
                "ticker": getattr(trade.contract, "symbol", "") or "",
                "side": str(order.action or ""),
            }
            if ref:
                # Don't let an empty completed-order status clobber execution data.
                existing = by_ref.get(ref)
                if existing and float(existing.get("filled_qty") or 0) > 0:
                    if float(entry["filled_qty"] or 0) <= 0:
                        entry["filled_qty"] = existing["filled_qty"]
                        entry["avg_price"] = existing["avg_price"]
                        entry["status"] = entry["status"] or existing["status"]
                        entry["remaining"] = 0.0
                    if not entry["broker_order_id"]:
                        entry["broker_order_id"] = existing.get("broker_order_id") or ""
                by_ref[ref] = entry
            if oid:
                by_id[oid] = entry

        out: dict[str, dict[str, Any]] = {}
        for query in queries:
            client_id = str(query.get("client_order_id") or "").strip()
            broker_id = str(query.get("broker_order_id") or "").strip()
            hit = (by_ref.get(client_id) if client_id else None) or (
                by_id.get(broker_id) if broker_id else None
            )
            if hit and client_id:
                out[client_id] = hit
            elif hit and broker_id:
                out[broker_id] = hit
        return out

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
            market=parse_market(getattr(proposal, "market", None) or "us"),
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
