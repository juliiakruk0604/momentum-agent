import pandas as pd

from src.store import SignalStore


def test_v25_point_in_time_base_join_never_uses_future(tmp_path):
    store = SignalStore(path=str(tmp_path / "pit.db"), database_url=None)

    event_ts = pd.Timestamp("2026-08-30T11:01:00Z")
    event_ms = int(event_ts.timestamp() * 1000)

    store.upsert_v22_flow_snapshot({
        "snapshot_time": "2026-08-30T11:00:30+00:00",
        "symbol": "BTCUSDT",
        "signal_time": "2026-08-30T11:00:00+00:00",
        "signal_price": 100.0,
        "score": 71.0,
        "regime": "CHOP",
        "action": "WATCH",
        "fast_features": {"ret_3m_pct": 0.1},
    })
    store.upsert_v22_flow_snapshot({
        "snapshot_time": "2026-08-30T11:01:30+00:00",
        "symbol": "BTCUSDT",
        "signal_time": "2026-08-30T11:01:00+00:00",
        "signal_price": 101.0,
        "score": 99.0,
        "regime": "TREND_UP",
        "action": "TRADE",
        "fast_features": {"ret_3m_pct": 2.0},
    })

    store.upsert_v24_feature_snapshot({
        "snapshot_ms": event_ms,
        "symbol": "BTCUSDT",
        "best_bid": 100.0,
        "best_ask": 100.1,
        "mid": 100.05,
        "microstructure_score": 50.0,
        "regime": "CHOP",
    })
    store.upsert_v24_feature_label({
        "symbol": "BTCUSDT",
        "snapshot_ms": event_ms,
        "horizon_seconds": 300,
        "entry_ask": 100.1,
        "final_bid_return_pct": 0.5,
        "mfe_bid_pct": 0.7,
        "mae_bid_pct": -0.1,
        "hit_0_1": True,
        "hit_0_25": True,
        "hit_0_5": True,
        "hit_1": False,
        "hit_2": False,
        "label_version": "price_tick_v3_passage",
    })

    rows = store.v24_labeled_snapshots_with_base(
        300,
        limit=100,
        max_base_age_seconds=150,
    )
    assert len(rows) == 1
    base = rows[0]["snapshot"]["base_momentum"]
    assert base["score"] == 71.0
    assert base["signal_time"].startswith("2026-08-30 11:00")
    assert rows[0]["snapshot"]["base_momentum_source"] == "v22_point_in_time_join"
    assert base["normalized_momentum_source"] == "reconstructed_from_point_in_time_fast_features"
    assert base["normalized_momentum"]["ret_3m_over_rv"] > 0
