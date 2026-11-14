"""Lightweight unit checks for Bugbot guardrails that do not need the full stack."""

from trader.graph import _graph_lock, invoke_graph_locked
from trader.store import list_open_proposal_tickers_sync


def test_graph_lock_exists():
    assert _graph_lock is not None
    assert callable(invoke_graph_locked)


def test_open_proposal_tickers_helper_callable():
    # Returns a set even if Postgres is empty / unreachable in some envs.
    tickers = list_open_proposal_tickers_sync()
    assert isinstance(tickers, set)
