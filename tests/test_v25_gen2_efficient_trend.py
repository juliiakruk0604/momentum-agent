from src.v25.evidence import _research_efficient_trend_continuation


def strong_slow_state():
    return {
        "snapshot_ms": 1788091200000,
        "base_momentum": {
            "signal_time": "2026-08-30 12:00:00+00:00",
            "fast_features": {
                "ret_3m_pct": 0.30,
                "current_move_pct": 0.20,
                "trend_efficiency_30m": 0.70,
                "amihud_30m_pct_per_m_usdt": 0.01,
            },
            "normalized_momentum": {
                "ret_3m_over_rv": 1.20,
                "rs_5m_over_rv": 0.80,
            },
            "cross_section": {
                "trend_efficiency_30m_percentile": 0.90,
                "amihud_30m_percentile": 0.30,
            },
        },
    }


def test_gen2_efficient_trend_accepts_directional_liquid_move():
    assert _research_efficient_trend_continuation(strong_slow_state())


def test_gen2_efficient_trend_rejects_illiquid_move():
    s = strong_slow_state()
    s["base_momentum"]["cross_section"]["amihud_30m_percentile"] = 0.90
    assert not _research_efficient_trend_continuation(s)


def test_gen2_efficient_trend_rejects_choppy_move():
    s = strong_slow_state()
    s["base_momentum"]["cross_section"]["trend_efficiency_30m_percentile"] = 0.40
    assert not _research_efficient_trend_continuation(s)
