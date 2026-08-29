from types import SimpleNamespace

import pandas as pd

import worker


class FakeStore:
    def __init__(self, rows=None):
        self.rows = list(rows or [])
        self.inserted = []

    def recent(self, limit=250):
        return self.rows[-limit:]

    def upsert_impulse(self, impulse):
        self.inserted.append(impulse)
        self.rows.append({"symbol": impulse.symbol, "signal_time": str(impulse.signal_time)})


def test_store_impulse_if_new_rejects_duplicate_signal():
    signal_time = pd.Timestamp("2026-08-29T11:15:00Z")
    store = FakeStore([{"symbol": "USELESSUSDT", "signal_time": str(signal_time)}])
    impulse = SimpleNamespace(symbol="USELESSUSDT", signal_time=signal_time)

    inserted = worker._store_impulse_if_new(store, impulse)

    assert inserted is False
    assert store.inserted == []


def test_store_impulse_if_new_inserts_new_signal():
    signal_time = pd.Timestamp("2026-08-29T11:30:00Z")
    store = FakeStore([{"symbol": "USELESSUSDT", "signal_time": "2026-08-29 11:15:00+00:00"}])
    impulse = SimpleNamespace(symbol="USELESSUSDT", signal_time=signal_time)

    inserted = worker._store_impulse_if_new(store, impulse)

    assert inserted is True
    assert store.inserted == [impulse]
