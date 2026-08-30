from datetime import datetime, timezone

from src.v25.hybrid import hybrid_gate


def _feature(regime_ready=True):
    return {
        "symbol":"AAVEUSDT",
        "microstructure_score":72,
        "spread_pct":0.02,
        "bid_depth_5_usdt":1000,
        "ask_depth_5_usdt":1200,
        "trade_1s":{"buy_ratio":0.60},
        "trade_5s":{"buy_ratio":0.58},
        "sequence_context":{
            "score_persistence_3s":0.75,
            "buy_ratio_mean_3s":0.60,
            "book_imbalance_mean_3s":0.10,
        },
        "cross_exchange":{
            "binance_available":True,
            "okx_available":True,
            "external_minus_bybit_move_5s_pct":0.01,
        },
        "perp_context":{"oi_change_30s_pct":0.05},
        "base_momentum":{
            "signal_time":datetime.now(timezone.utc).isoformat(),
            "score":82,
            "initial_stop_pct":0.70,
            "risk":{"target_pct":2.0},
            "fast_features":{
                "ret_3m_pct":0.25,
                "rs_5m_pct":0.30,
                "volume_acceleration":2.0,
                "current_move_pct":0.15,
                "price_acceleration":0.05,
            },
        },
    }


def test_v25_hybrid_gate_accepts_strong_base_plus_micro(monkeypatch):
    monkeypatch.setenv("V25_MIN_DEPTH_USDT","500")
    monkeypatch.setenv("V25_MIN_MICRO_SCORE","60")
    monkeypatch.setenv("V25_MIN_SCORE_PERSISTENCE_3S","0.34")
    monkeypatch.setenv("V25_MIN_BUY_RATIO_MEAN_3S","0.52")
    assert hybrid_gate(_feature(), "TREND_UP") == []


def test_v25_hybrid_gate_blocks_downtrend():
    blockers = hybrid_gate(_feature(), "TREND_DOWN")
    assert "market_trend_down" in blockers


def test_v25_hybrid_gate_requires_primary_alpha():
    f = _feature()
    f["base_momentum"] = None
    blockers = hybrid_gate(f, "TREND_UP")
    assert "no_base_momentum" in blockers
