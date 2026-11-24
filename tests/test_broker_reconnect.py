import threading

import pytest

from trader.broker import BrokerError, IBKRAdapter


def test_disconnect_error_detection():
    assert IBKRAdapter._is_disconnect_error(RuntimeError("Not connected"))
    assert IBKRAdapter._is_disconnect_error(TimeoutError("peer closed connection"))
    assert IBKRAdapter._is_disconnect_error(
        RuntimeError("Unable to connect as the client id is already in use")
    )
    assert IBKRAdapter._is_disconnect_error(
        OSError(-3, "Temporary failure in name resolution")
    )
    assert not IBKRAdapter._is_disconnect_error(RuntimeError("Insufficient cash"))


def test_reconnect_cooldown_fails_fast():
    adapter = IBKRAdapter.__new__(IBKRAdapter)
    adapter._intentional_mock = False
    adapter._ib = None
    adapter._loop = None
    adapter._ib_thread = None
    adapter._connect_lock = threading.RLock()
    adapter._reconnect_cooldown_seconds = 15.0
    adapter._last_reconnect_error = None
    adapter._reconnect_cooldown_until = 0.0
    adapter._mark_reconnect_failure(RuntimeError("Temporary failure in name resolution"))
    with pytest.raises(BrokerError, match="cooling down"):
        adapter.ensure_connected()


def test_connect_lock_is_reentrant_for_disconnect_cleanup():
    """Regression: connect-path cleanup used to deadlock on a non-reentrant lock."""
    adapter = IBKRAdapter.__new__(IBKRAdapter)
    adapter._ib = None
    adapter._loop = None
    adapter._ib_thread = None
    adapter._connect_lock = threading.RLock()
    adapter._intentional_mock = False

    with adapter._connect_lock:
        # Same-thread nested acquire is what _start_ib_thread hits via disconnect().
        adapter.disconnect()


def test_client_id_rotates_within_window():
    from trader.config import Settings

    adapter = IBKRAdapter.__new__(IBKRAdapter)
    adapter.settings = Settings.model_construct(ibkr_client_id=7)
    adapter._client_id_offset = 0
    assert adapter._next_client_id() == 7
    adapter._client_id_offset = 1
    assert adapter._next_client_id() == 8
    adapter._client_id_offset = 5
    assert adapter._next_client_id() == 7
