import pandas as pd

from src.v22.engine import _closed_1m
from src.v22.orderflow import flow_score
from src.v22.runner import evaluate_runner_path


def test_forming_1m_bar_is_excluded():
    idx = pd.to_datetime([
        "2026-08-30T09:00:00Z",
        "2026-08-30T09:01:00Z",
    ])
    frame = pd.DataFrame([
        {"open":1,"high":1.1,"low":0.9,"close":1.0,"volume":1,"turnover":1},
        {"open":1,"high":2.0,"low":1.0,"close":2.0,"volume":10,"turnover":10},
    ], index=idx)
    closed = _closed_1m(frame, pd.Timestamp("2026-08-30T09:01:30Z"))
    assert list(closed.index) == [idx[0]]


def test_positive_orderflow_scores_higher():
    book = {
        "bids":[(100,20),(99.9,10),(99.8,10),(99.7,5),(99.6,5)],
        "asks":[(100.1,5),(100.2,3),(100.3,3),(100.4,2),(100.5,2)],
    }
    trades = [
        {"time":1000+i*100,"price":100.1,"size":2,"side":"Buy"}
        for i in range(20)
    ] + [
        {"time":4000+i*100,"price":100.0,"size":0.5,"side":"Sell"}
        for i in range(5)
    ]
    score, bf, tf = flow_score(book, trades)
    assert score > 50
    assert bf["book_imbalance"] > 0
    assert tf["buy_ratio"] > 0.5


def test_runner_stop_first_same_bar():
    idx = pd.to_datetime(["2026-01-01T00:00:00Z"])
    bars = pd.DataFrame([
        {"open":100,"high":105,"low":98.5,"close":104,"volume":1,"turnover":100}
    ], index=idx)
    out = evaluate_runner_path(
        bars1m=bars,
        entry_time=idx[0],
        entry_price=100,
        notional_usdt=5,
        initial_stop_pct=1.0,
        entry_slippage_pct=0,
        exit_slippage_pct=0,
        partial_r=1.5,
        partial_fraction=0.5,
        trail_pct=1.0,
        breakeven_buffer_pct=0.2,
        max_hold_minutes=10,
    )
    assert out.status == "CLOSED"
    assert out.partial_hit is False
    assert out.final_exit_reason == "STOP"


def test_runner_can_leave_open_after_partial():
    idx = pd.to_datetime([
        "2026-01-01T00:00:00Z",
        "2026-01-01T00:01:00Z",
    ])
    bars = pd.DataFrame([
        {"open":100,"high":100.8,"low":99.5,"close":100.5,"volume":1,"turnover":100},
        {"open":100.5,"high":102.0,"low":100.6,"close":101.8,"volume":1,"turnover":100},
    ], index=idx)
    out = evaluate_runner_path(
        bars1m=bars,
        entry_time=idx[0],
        entry_price=100,
        notional_usdt=5,
        initial_stop_pct=1.0,
        mark_price=101.8,
        entry_slippage_pct=0,
        exit_slippage_pct=0,
        partial_r=1.5,
        partial_fraction=0.5,
        trail_pct=1.2,
        breakeven_buffer_pct=0.2,
        max_hold_minutes=10,
    )
    assert out.partial_hit is True
    assert out.status in ("OPEN","CLOSED")
