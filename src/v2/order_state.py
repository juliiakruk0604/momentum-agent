from __future__ import annotations

from dataclasses import dataclass, asdict
from enum import Enum


class OrderState(str, Enum):
    SIGNAL = "SIGNAL"
    PLANNED = "PLANNED"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    EXIT_PENDING = "EXIT_PENDING"
    CLOSED = "CLOSED"
    CANCELED = "CANCELED"
    REJECTED = "REJECTED"


_ALLOWED = {
    OrderState.SIGNAL: {OrderState.PLANNED, OrderState.REJECTED},
    OrderState.PLANNED: {OrderState.SUBMITTED, OrderState.CANCELED, OrderState.REJECTED},
    OrderState.SUBMITTED: {OrderState.PARTIALLY_FILLED, OrderState.FILLED, OrderState.CANCELED, OrderState.REJECTED},
    OrderState.PARTIALLY_FILLED: {OrderState.FILLED, OrderState.CANCELED, OrderState.EXIT_PENDING},
    OrderState.FILLED: {OrderState.EXIT_PENDING},
    OrderState.EXIT_PENDING: {OrderState.CLOSED, OrderState.CANCELED},
    OrderState.CLOSED: set(),
    OrderState.CANCELED: set(),
    OrderState.REJECTED: set(),
}


@dataclass
class OrderLifecycle:
    client_id: str
    symbol: str
    state: str
    filled_qty: float = 0.0
    avg_price: float | None = None
    exchange_order_id: str | None = None
    reason: str | None = None

    def transition(self, next_state: str, **updates):
        current = OrderState(self.state)
        nxt = OrderState(next_state)
        if nxt not in _ALLOWED[current]:
            raise RuntimeError(f"invalid_order_transition:{current.value}->{nxt.value}")
        self.state = nxt.value
        for key, value in updates.items():
            if hasattr(self, key):
                setattr(self, key, value)
        return self

    def to_dict(self):
        return asdict(self)
