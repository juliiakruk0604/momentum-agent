import pandas as pd

from src.v2.simulator import find_barrier_exit


def test_same_bar_stop_wins_conservatively():
    idx = pd.to_datetime(["2026-01-01T00:00:00Z"])
    bars = pd.DataFrame([{"open":100,"high":106,"low":94,"close":101,"volume":1,"turnover":100}], index=idx)
    event = find_barrier_exit(bars, idx[0], 95, 105, exit_slippage_pct=0)
    assert event["exit_reason"] == "STOP"
    assert event["exit_price"] == 95


def test_no_barrier_returns_none_without_timeout():
    idx = pd.to_datetime(["2026-01-01T00:00:00Z"])
    bars = pd.DataFrame([{"open":100,"high":101,"low":99,"close":100,"volume":1,"turnover":100}], index=idx)
    event = find_barrier_exit(bars, idx[0], 95, 105, max_hold_minutes=None)
    assert event is None
