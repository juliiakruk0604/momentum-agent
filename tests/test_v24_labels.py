import pytest
import time

from src.store import SignalStore
from src.v24.labels import label_from_path


def test_v24_label_uses_entry_ask_and_future_bid():
    snapshot = {
        "symbol": "BTCUSDT",
        "snapshot_ms": 1000,
        "best_ask": 100.10,
    }
    path = [
        {"snapshot_ms": 3000, "best_bid": 100.20},
        {"snapshot_ms": 6000, "best_bid": 100.50},
    ]
    out = label_from_path(snapshot, path, 5)
    assert out["entry_ask"] == 100.10
    assert out["final_bid"] == 100.50
    assert out["mfe_bid_pct"] > 0
    assert out["mae_bid_pct"] > 0


def test_v24_label_rejects_incomplete_future_path(monkeypatch):
    monkeypatch.setenv("V24_LABEL_END_TOLERANCE_MS", "1000")
    snapshot = {
        "symbol": "BTCUSDT",
        "snapshot_ms": 1000,
        "best_ask": 100.10,
    }
    path = [
        {"snapshot_ms": 2500, "best_bid": 100.20},
    ]
    with pytest.raises(RuntimeError, match="future_path_incomplete"):
        label_from_path(snapshot, path, 5)


def test_v24_snapshot_and_label_store(tmp_path):
    store = SignalStore(path=str(tmp_path / "v24.db"), database_url=None)
    store.upsert_v24_feature_snapshot({
        "snapshot_ms": 1000,
        "symbol": "BTCUSDT",
        "best_bid": 100.0,
        "best_ask": 100.1,
        "mid": 100.05,
        "microstructure_score": 80.0,
        "regime": "TREND_UP",
        "perp_context": {"funding_rate": 0.0001},
        "cross_exchange": {"external_minus_bybit_move_1s_pct": 0.02},
    })
    store.upsert_v24_feature_snapshot({
        "snapshot_ms": 6000,
        "symbol": "BTCUSDT",
        "best_bid": 100.5,
        "best_ask": 100.6,
        "mid": 100.55,
        "microstructure_score": 85.0,
        "regime": "TREND_UP",
    })
    stats = store.v24_feature_snapshot_stats()
    assert stats["snapshots"] == 2
    path = store.v24_feature_path("BTCUSDT", 1000, 6000)
    assert len(path) == 1
    label = label_from_path(
        {
            "symbol": "BTCUSDT",
            "snapshot_ms": 1000,
            "best_ask": 100.1,
        },
        path,
        5,
    )
    store.upsert_v24_feature_label(label)
    label_stats = store.v24_feature_label_stats()
    assert label_stats[0]["horizon_seconds"] == 5
    assert label_stats[0]["n"] == 1


def test_v24_batched_price_tape(tmp_path):
    store = SignalStore(path=str(tmp_path / "v24_ticks.db"), database_url=None)
    n = store.insert_v24_price_ticks_batch(1000, [
        {"symbol":"BTCUSDT","best_bid":100.0,"best_ask":100.1,"mid":100.05},
        {"symbol":"ETHUSDT","best_bid":50.0,"best_ask":50.1,"mid":50.05},
    ])
    assert n == 2
    store.insert_v24_price_ticks_batch(2000, [
        {"symbol":"BTCUSDT","best_bid":100.2,"best_ask":100.3,"mid":100.25},
        {"symbol":"ETHUSDT","best_bid":50.2,"best_ask":50.3,"mid":50.25},
    ])
    path = store.v24_price_path("BTCUSDT", 1000, 2000)
    assert len(path) == 1
    assert path[0]["best_bid"] == 100.2
    assert store.v24_price_tick_stats()["ticks"] == 4


def test_priority_candidates_are_one_per_symbol_and_horizon_spaced(tmp_path):
    store = SignalStore(path=str(tmp_path / "priority.db"), database_url=None)
    horizon = 5
    start_ms = int(time.time() * 1000) - 30_000
    for symbol in ("BTCUSDT", "ETHUSDT"):
        for offset in (0, 1_000, 5_000):
            snapshot_ms = start_ms + offset
            store.upsert_v24_feature_snapshot({
                "snapshot_ms": snapshot_ms,
                "symbol": symbol,
                "best_bid": 100.0,
                "best_ask": 100.1,
                "mid": 100.05,
                "microstructure_score": 50.0,
            })
            store.insert_v24_price_ticks_batch(snapshot_ms + horizon * 1000, [{
                "symbol": symbol,
                "best_bid": 100.2,
                "best_ask": 100.3,
                "mid": 100.25,
            }])

    first = store.v24_independent_label_candidates(
        horizon,
        limit=10,
        min_snapshot_ms=start_ms,
    )
    assert {(row["symbol"], row["snapshot_ms"]) for row in first} == {
        ("BTCUSDT", start_ms),
        ("ETHUSDT", start_ms),
    }

    for row in first:
        store.upsert_v24_feature_label({
            "symbol": row["symbol"],
            "snapshot_ms": row["snapshot_ms"],
            "horizon_seconds": horizon,
            "entry_ask": 100.1,
            "final_bid_return_pct": 0.1,
            "mfe_bid_pct": 0.2,
            "mae_bid_pct": -0.1,
            "label_version": "price_tick_v3_passage",
        })

    second = store.v24_independent_label_candidates(
        horizon,
        limit=10,
        min_snapshot_ms=start_ms,
    )
    assert {(row["symbol"], row["snapshot_ms"]) for row in second} == {
        ("BTCUSDT", start_ms + 5_000),
        ("ETHUSDT", start_ms + 5_000),
    }
