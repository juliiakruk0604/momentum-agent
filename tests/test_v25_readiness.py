from src.v25.readiness import evaluate_v25_readiness


class Store:
    def __init__(self, data):
        self.data = data

    def get_runtime(self, key):
        value = self.data.get(key)
        return None if value is None else {"value": value}


def _trades(n=40, pnl=0.02):
    return [
        {
            "pnl_usdt": pnl,
            "perp_1x_counterfactual_pnl_usdt": pnl + 0.001,
        }
        for _ in range(n)
    ]


def _healthy_store(trades=None):
    return Store({
        "v25_hybrid_shadow_summary": {
            "recent_trades": trades if trades is not None else _trades(),
            "diagnostics": {"base_momentum_attached": 4},
        },
        "v25_evidence": {
            "promotion": {"candidate_promotable": True, "reasons": []},
        },
        "v24_stream_status": {
            "connected": True,
            "last_message_ms": 10**15,
            "linear_stream": {"connected": True},
            "binance_stream": {"connected": True},
            "okx_stream": {"connected": True},
        },
    })


def test_candidate_ready_requires_more_than_positive_average(monkeypatch):
    monkeypatch.setenv("V25_MAX_STREAM_AGE_MS", "999999999999999")
    store = _healthy_store(_trades(40, 0.02))
    result = evaluate_v25_readiness(store)
    assert result["candidate_ready"] is True
    assert result["live_ready"] is False
    assert result["live_execution"] is False


def test_candidate_blocked_on_small_forward_sample(monkeypatch):
    monkeypatch.setenv("V25_MAX_STREAM_AGE_MS", "999999999999999")
    result = evaluate_v25_readiness(_healthy_store(_trades(10, 0.02)))
    assert result["candidate_ready"] is False
    assert "insufficient_forward_trades" in result["blockers"]


def test_candidate_blocked_when_purged_validation_fails(monkeypatch):
    monkeypatch.setenv("V25_MAX_STREAM_AGE_MS", "999999999999999")
    store = _healthy_store()
    store.data["v25_evidence"] = {
        "promotion": {"candidate_promotable": False, "reasons": ["spot_net_nonpositive"]}
    }
    result = evaluate_v25_readiness(store)
    assert result["candidate_ready"] is False
    assert "purged_validation_not_promotable" in result["blockers"]


def test_live_never_enabled_even_when_candidate_ready(monkeypatch):
    monkeypatch.setenv("V25_MAX_STREAM_AGE_MS", "999999999999999")
    result = evaluate_v25_readiness(_healthy_store())
    assert result["candidate_ready"] is True
    assert result["live_ready"] is False
    assert result["live_execution"] is False
