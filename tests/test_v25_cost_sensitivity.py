from src.v25.evidence import _hypothesis_promotion, _metrics, RESEARCH_HYPOTHESES


def test_v25_research_hypotheses_registered():
    assert set(RESEARCH_HYPOTHESES) == {
        "cross_section_momentum",
        "volume_breakout",
        "normalized_continuation",
        "liquidation_reversal",
        "short_squeeze_continuation",
    }


def test_cost_sensitivity_monotonic():
    rows = [
        {"label":{"final_bid_return_pct":0.50}},
        {"label":{"final_bid_return_pct":0.30}},
    ]
    m = _metrics(rows)
    cs = m["cost_sensitivity_pct"]
    assert cs["0.1"] > cs["0.17"] > cs["0.26"] > cs["0.35"]


def test_hypothesis_promotion_requires_all_evidence(monkeypatch):
    monkeypatch.setenv("V25_HYPOTHESIS_MIN_VALIDATION_N", "15")
    monkeypatch.setenv("V25_HYPOTHESIS_MIN_PROFIT_FACTOR", "1.2")
    metrics = {
        "n": 20,
        "avg_spot_net_pct": 0.10,
        "spot_lower_mean_90_pct": -0.01,
        "spot_profit_factor": 1.5,
    }
    result = _hypothesis_promotion(metrics, lift_vs_base=0.03)
    assert result["candidate_promotable"] is False
    assert result["reasons"] == ["lower_mean_90_nonpositive"]


def test_all_win_profit_factor_uses_json_safe_cap():
    metrics = _metrics([
        {"label":{"final_bid_return_pct":0.50}},
        {"label":{"final_bid_return_pct":0.40}},
    ])
    assert metrics["spot_profit_factor"] == 999.0
    assert metrics["spot_profit_factor_no_losses"] is True
