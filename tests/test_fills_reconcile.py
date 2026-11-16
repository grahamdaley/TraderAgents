from trader.fills import classify_order_outcome


def test_classify_fully_filled():
    assert (
        classify_order_outcome(
            broker_status="Filled", filled_qty=10, avg_price=100.0, remaining=0
        )
        == "filled"
    )


def test_classify_partial_then_cancelled_counts_as_fill():
    assert (
        classify_order_outcome(
            broker_status="Cancelled", filled_qty=4, avg_price=99.5, remaining=6
        )
        == "filled"
    )


def test_classify_cancelled_unfilled():
    assert (
        classify_order_outcome(
            broker_status="Cancelled", filled_qty=0, avg_price=0, remaining=10
        )
        == "cancelled"
    )


def test_classify_still_working():
    assert (
        classify_order_outcome(
            broker_status="Submitted", filled_qty=0, avg_price=0, remaining=10
        )
        is None
    )
