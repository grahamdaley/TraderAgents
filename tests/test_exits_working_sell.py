from types import SimpleNamespace

from trader.exits import working_sell_tickers


def test_working_sell_tickers_from_open_orders():
    book = SimpleNamespace(
        open_orders=[
            {"ticker": "MRK", "side": "SELL", "quantity": 34},
            {"ticker": "MSFT", "side": "BUY", "quantity": 9},
            {"ticker": "aapl", "side": "sell", "quantity": 1},
            {"ticker": "", "side": "SELL", "quantity": 1},
        ]
    )
    assert working_sell_tickers(book) == {"MRK", "AAPL"}


def test_working_sell_tickers_empty_book():
    assert working_sell_tickers(SimpleNamespace(open_orders=None)) == set()
    assert working_sell_tickers(SimpleNamespace(open_orders=[])) == set()
