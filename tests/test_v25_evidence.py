from src.v25.evidence import _base_ok, _micro_ok, _nonoverlap, run_v25_evidence


def _snapshot():
    return {
        "snapshot_ms": 1788087000000,
        "regime": "CHOP",
        "base_momentum": {
            "signal_time": "2026-08-30 10:49:00+00:00",
            "score": 80,
            "fast_features": {
                "ret_3m_pct": 0.30,
                "rs_5m_pct": 0.25,
                "volume_acceleration": 2.0,
                "price_acceleration": 0.05,
                "current_move_pct": 0.10,
            },
            "cross_section": {
                "composite_percentile": 0.90,
                "rs_5m_percentile": 0.85,
            },
        },
        "microstructure_score": 72,
        "spread_pct": 0.02,
        "bid_depth_5_usdt": 1500,
        "ask_depth_5_usdt": 1700,
        "trade_5s": {"buy_ratio": 0.60},
        "sequence_context": {
            "score_persistence_3s": 0.75,
            "buy_ratio_mean_3s": 0.60,
            "book_imbalance_mean_3s": 0.10,
        },
    }


def test_v25_evidence_base_and_micro_layers(monkeypatch):
    s = _snapshot()
    monkeypatch.setenv("V25_MAX_BASE_SIGNAL_AGE_SECONDS", "100000")
    assert _base_ok(s)
    assert _micro_ok(s)
    s["microstructure_score"] = 20
    assert _base_ok(s)
    assert not _micro_ok(s)


def test_v25_evidence_base_rejects_weak_alpha(monkeypatch):
    s = _snapshot()
    monkeypatch.setenv("V25_MAX_BASE_SIGNAL_AGE_SECONDS", "100000")
    s["base_momentum"]["score"] = 40
    assert not _base_ok(s)


def test_v25_evidence_nonoverlap_per_symbol():
    rows = [
        {"symbol":"A","snapshot_ms":1000},
        {"symbol":"A","snapshot_ms":2000},
        {"symbol":"A","snapshot_ms":7000},
        {"symbol":"B","snapshot_ms":2000},
    ]
    out = _nonoverlap(rows, 5)
    assert [(x["symbol"], x["snapshot_ms"]) for x in out] == [
        ("A",1000),("B",2000),("A",7000)
    ]


def test_v25_promotion_gate_fails_closed_without_validation_evidence(monkeypatch):
    class EmptyStore:
        def __init__(self):
            self.runtime = None

        def v24_labeled_snapshots(self, horizon, limit=30000):
            return []

        def set_runtime(self, key, value):
            self.runtime = (key, value)

    monkeypatch.setenv("V25_PROMOTION_MIN_VALIDATION_N", "20")
    store = EmptyStore()
    result = run_v25_evidence(store)

    promotion = result["promotion"]
    assert promotion["candidate_promotable"] is False
    assert promotion["required_horizons"] == [900, 1800]
    for horizon in promotion["required_horizons"]:
        assert f"h{horizon}:insufficient_validation_n" in promotion["reasons"]
        assert f"h{horizon}:spot_net_nonpositive" in promotion["reasons"]
        assert f"h{horizon}:no_lift_vs_base" in promotion["reasons"]

    assert store.runtime == ("v25_evidence", result)
