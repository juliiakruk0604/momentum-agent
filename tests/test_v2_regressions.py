import os

import pandas as pd

from research_service import _ensure_v2_provenance
from src.v2.backtest import V2BacktestRunner, _strategy_config_snapshot, _strategy_fingerprint
from src.v2.engine import _closed_15m
from src.v2.risk import cost_adjusted_levels
from src.v2.shadow import _signal_key


class _Store:
    def __init__(self):
        self.values = {}

    def get_runtime(self, key):
        if key not in self.values:
            return None
        return {"value": self.values[key]}

    def set_runtime(self, key, value):
        self.values[key] = value


class _Provider:
    def liquid_spot_usdt_symbols(self, limit, min_turnover):
        return ["BTCUSDT"]


def test_v2_provenance_guard_creates_stable_verified_state(monkeypatch):
    monkeypatch.setenv("V2_BACKTEST_UNIVERSE", "1")
    store = _Store()
    runner = V2BacktestRunner(store, provider=_Provider())

    state = _ensure_v2_provenance(runner)

    assert state["strategy_fingerprint"] == _strategy_fingerprint()
    assert state["strategy_config"] == _strategy_config_snapshot()
    dataset_id = state["dataset_id"]
    runner.ensure_state()
    assert runner.state()["dataset_id"] == dataset_id
    assert runner.state()["cursor"] == 0


def test_v2_provenance_guard_never_backfills_missing_provenance_in_place(monkeypatch):
    monkeypatch.setenv("V2_BACKTEST_UNIVERSE", "1")
    store = _Store()
    runner = V2BacktestRunner(store, provider=_Provider())
    old_state = runner._new_state()
    old_id = old_state["dataset_id"]
    old_state["cursor"] = 1
    store.set_runtime(runner.STATE_KEY, old_state)

    state = _ensure_v2_provenance(runner)

    assert f"v2_backtest_superseded:{old_id}" in store.values
    assert store.values[f"v2_backtest_superseded:{old_id}"]["cursor"] == 1
    assert state["cursor"] == 0
    assert state["strategy_fingerprint"] == _strategy_fingerprint()


def test_cost_adjusted_rr_meets_gate_without_float_false_negative(monkeypatch):
    monkeypatch.setenv("V2_FEE_RATE", "0.001")
    monkeypatch.setenv("V2_ENTRY_SLIPPAGE_PCT", "0.05")
    monkeypatch.setenv("V2_EXIT_SLIPPAGE_PCT", "0.05")
    monkeypatch.setenv("V2_MIN_NET_RR", "1.60")
    monkeypatch.setenv("V2_MAX_TARGET_PCT", "6.0")
    levels = cost_adjusted_levels(0.8, 1.76, {"spread_pct": 0.012})
    assert levels["blocked"] is False
    assert levels["net_rr"] >= 1.60 - 1e-9
    assert levels["target_pct"] > 1.76


def test_forming_15m_bar_is_excluded():
    idx = pd.to_datetime([
        "2026-08-30T08:30:00Z",
        "2026-08-30T08:45:00Z",
    ])
    frame = pd.DataFrame(
        [
            {"open": 1, "high": 2, "low": 1, "close": 2, "volume": 1, "turnover": 1},
            {"open": 2, "high": 3, "low": 2, "close": 3, "volume": 1, "turnover": 1},
        ],
        index=idx,
    )
    closed = _closed_15m(frame, pd.Timestamp("2026-08-30T08:53:00Z"))
    assert list(closed.index) == [idx[0]]


def test_signal_key_uses_signal_bar_not_scan_time():
    candidate = {
        "symbol": "HYPEUSDT",
        "setup": "MOMENTUM_BREAKOUT",
        "features": {"signal_time": "2026-08-30 08:30:00+00:00"},
    }
    a = _signal_key({"generated_at": "2026-08-30 08:47:00+00:00"}, candidate)
    b = _signal_key({"generated_at": "2026-08-30 08:53:00+00:00"}, candidate)
    assert a == b
