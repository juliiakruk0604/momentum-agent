from src.v25.evidence import (
    _research_cross_section_momentum,
    _research_volume_breakout,
    _research_normalized_continuation,
)


def sample():
    return {
        "snapshot_ms": 1788087000000,
        "base_momentum": {
            "signal_time": "2026-08-30 10:49:00+00:00",
            "score": 80,
            "fast_features": {
                "ret_3m_pct": 0.30,
                "ret_5m_pct": 0.50,
                "rs_5m_pct": 0.35,
                "volume_acceleration": 2.2,
                "price_acceleration": 0.04,
                "current_move_pct": 0.20,
            },
            "cross_section": {
                "composite_percentile": 0.90,
                "rs_5m_percentile": 0.85,
                "volume_accel_percentile": 0.90,
            },
            "normalized_momentum": {
                "ret_3m_over_rv": 1.4,
                "ret_5m_over_rv": 1.3,
                "rs_5m_over_rv": 1.1,
            },
        },
    }


def test_fixed_momentum_hypotheses(monkeypatch):
    monkeypatch.setenv("V25_MAX_BASE_SIGNAL_AGE_SECONDS", "100000")
    s = sample()
    assert _research_cross_section_momentum(s)
    assert _research_volume_breakout(s)
    assert _research_normalized_continuation(s)


def test_cross_section_family_rejects_low_rank(monkeypatch):
    monkeypatch.setenv("V25_MAX_BASE_SIGNAL_AGE_SECONDS", "100000")
    s = sample()
    s["base_momentum"]["cross_section"]["composite_percentile"] = 0.4
    assert not _research_cross_section_momentum(s)
