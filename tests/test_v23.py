import pandas as pd

from src.v23.confirm import confirmation_decision


def base_candidate():
    return {
        "symbol": "HYPEUSDT",
        "action": "SHADOW_READY",
        "score": 84.0,
        "flow_score": 68.0,
        "signal_price": 100.2,
        "trade_flow": {
            "flow_confidence": 0.9,
            "recent_buy_ratio": 0.68,
        },
        "book": {
            "spread_pct": 0.02,
            "depth_usdt": 20000,
        },
        "flow_acceleration": {
            "flow_score_delta": 2.0,
        },
    }


def test_v23_requires_time_separation():
    armed = {
        "symbol": "HYPEUSDT",
        "armed_at": "2026-08-30T09:00:00Z",
        "signal_price": 100.0,
        "score": 86.0,
    }
    d = confirmation_decision(
        armed,
        base_candidate(),
        now=pd.Timestamp("2026-08-30T09:00:10Z"),
    )
    assert d["confirmed"] is False
    assert "confirmation_too_early" in d["reasons"]


def test_v23_confirms_persistent_impulse():
    armed = {
        "symbol": "HYPEUSDT",
        "armed_at": "2026-08-30T09:00:00Z",
        "signal_price": 100.0,
        "score": 86.0,
    }
    d = confirmation_decision(
        armed,
        base_candidate(),
        now=pd.Timestamp("2026-08-30T09:01:00Z"),
    )
    assert d["confirmed"] is True


def test_v23_rejects_flow_collapse():
    armed = {
        "symbol": "HYPEUSDT",
        "armed_at": "2026-08-30T09:00:00Z",
        "signal_price": 100.0,
        "score": 86.0,
    }
    c = base_candidate()
    c["flow_score"] = 20.0
    c["trade_flow"]["recent_buy_ratio"] = 0.40
    d = confirmation_decision(
        armed,
        c,
        now=pd.Timestamp("2026-08-30T09:01:00Z"),
    )
    assert d["confirmed"] is False
    assert "flow_low" in d["reasons"]
    assert "buy_ratio_low" in d["reasons"]
