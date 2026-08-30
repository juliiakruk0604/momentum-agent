import pytest

from src.v2.order_state import OrderLifecycle, OrderState


def test_valid_order_lifecycle():
    order = OrderLifecycle("x","BTCUSDT",OrderState.SIGNAL.value)
    order.transition(OrderState.PLANNED.value)
    order.transition(OrderState.SUBMITTED.value)
    order.transition(OrderState.FILLED.value, filled_qty=1)
    order.transition(OrderState.EXIT_PENDING.value)
    order.transition(OrderState.CLOSED.value)
    assert order.state == "CLOSED"


def test_invalid_transition_fails_closed():
    order = OrderLifecycle("x","BTCUSDT",OrderState.SIGNAL.value)
    with pytest.raises(RuntimeError):
        order.transition(OrderState.FILLED.value)
