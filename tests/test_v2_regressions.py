import os

import pandas as pd

from src.v2.engine import _closed_15m
from src.v2.risk import cost_adjusted_levels
from src.v2.shadow import _signal_key


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
