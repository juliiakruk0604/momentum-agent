from src.v25.evidence import _metrics, RESEARCH_HYPOTHESES


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
