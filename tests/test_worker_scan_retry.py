import pandas as pd

import worker


class FakeProvider:
    def __init__(self):
        self.calls = {}

    def kline(self, symbol, interval, limit):
        self.calls[symbol] = self.calls.get(symbol, 0) + 1
        if symbol == "FAILUSDT" and self.calls[symbol] == 1:
            raise RuntimeError("transient")
        return object()

    def liquid_usdt_symbols(self, limit):
        return ["OKUSDT", "FAILUSDT"]


class FakeStore:
    def __init__(self):
        self.runtime = {}

    def get_runtime(self, key):
        if key not in self.runtime:
            return None
        return {"value": self.runtime[key]}

    def set_runtime(self, key, value):
        self.runtime[key] = value

    def upsert_impulse(self, impulse):
        raise AssertionError("no impulses expected")

    def pending(self):
        return []

    def research_status(self):
        return {}

    def save_daily_snapshot(self, snapshot, today):
        pass


def test_run_once_retries_only_failed_market_symbols(monkeypatch):
    provider = FakeProvider()
    store = FakeStore()
    cfg = {"continuation": {"observation_minutes": 30}}

    monkeypatch.setattr(worker, "compute_impulse_candidates", lambda *args, **kwargs: [])
    monkeypatch.setattr(worker, "process_labels", lambda *args, **kwargs: {"labeled": 0, "label_errors": 0})
    monkeypatch.setattr(worker, "process_v2_scan", lambda *args, **kwargs: {"performed": False})
    monkeypatch.setattr(worker, "process_v2_shadow", lambda *args, **kwargs: {})
    monkeypatch.setattr(worker, "process_shadow_portfolio", lambda *args, **kwargs: {})
    monkeypatch.setattr(worker, "process_micro_live", lambda *args, **kwargs: {"action": "NO_TRADE"})
    monkeypatch.setattr(worker, "process_promo_scan", lambda *args, **kwargs: {"performed": False})

    summary = worker.run_once(provider, store, cfg, universe_limit=2)

    scan = store.runtime["market_scan_summary"]
    assert summary["scan_errors"] == 0
    assert scan["initial_scan_errors"] == 1
    assert scan["retry_recovered"] == 1
    assert scan["failed_symbols"] == []
    assert provider.calls["OKUSDT"] == 1
    assert provider.calls["FAILUSDT"] == 2
    assert provider.calls["BTCUSDT"] == 1
    assert provider.calls["ETHUSDT"] == 1
