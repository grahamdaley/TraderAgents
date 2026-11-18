from trader.broker import IBKRAdapter


def test_disconnect_error_detection():
    assert IBKRAdapter._is_disconnect_error(RuntimeError("Not connected"))
    assert IBKRAdapter._is_disconnect_error(TimeoutError("peer closed connection"))
    assert IBKRAdapter._is_disconnect_error(
        RuntimeError("Unable to connect as the client id is already in use")
    )
    assert not IBKRAdapter._is_disconnect_error(RuntimeError("Insufficient cash"))
