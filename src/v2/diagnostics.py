from __future__ import annotations


def _clip(x, lo=0.0, hi=1.0):
    return max(lo, min(hi, float(x)))


def diagnostic_strength(f, regime):
    # Pure diagnostics only. This score never authorizes a trade.
    score = (
        24 * _clip(max(f.breakout_atr, 0.0) / 1.2)
        + 24 * _clip(max(f.volume_z, 0.0) / 4.0)
        + 22 * _clip(max(f.rs_atr, 0.0) / 1.5)
        + 15 * _clip(max(f.bb_width_expansion - 0.8, 0.0) / 0.8)
        + 10 * _clip(max(f.ret_15m_pct, 0.0) / max(f.atr_pct, 1e-6))
        + (5 if regime.name.startswith("TREND_UP") else 0)
    )
    return round(score, 2)


def setup_blockers(f, regime):
    out = {}

    b = []
    if regime.name.startswith("TREND_DOWN"):
        b.append("trend_down")
    if f.breakout_atr < 0.35:
        b.append("breakout_atr_low")
    if f.volume_z < 1.8:
        b.append("volume_z_low")
    if f.rs_atr < 0.35:
        b.append("relative_strength_low")
    if f.vwap_distance_atr < 0.1:
        b.append("below_vwap_buffer")
    if f.vwap_distance_atr > 2.2:
        b.append("overextended")
    out["MOMENTUM_BREAKOUT"] = b

    b = []
    if regime.name.startswith("TREND_DOWN") or regime.name == "CHOP":
        b.append("regime_not_acceleration")
    if not (0.05 <= f.breakout_atr <= 0.55):
        b.append("breakout_stage_wrong")
    if f.volume_z < 1.4:
        b.append("volume_z_low")
    if f.rs_atr < 0.25:
        b.append("relative_strength_low")
    if f.ret_15m_pct <= 0:
        b.append("no_positive_acceleration")
    if f.bb_width_expansion < 1.0:
        b.append("volatility_not_expanding")
    if f.vwap_distance_atr < 0 or f.vwap_distance_atr > 1.6:
        b.append("vwap_location_bad")
    out["EARLY_ACCELERATION"] = b

    b = []
    if not regime.name.startswith("TREND_UP"):
        b.append("not_uptrend")
    if f.ret_4h_pct <= 0:
        b.append("four_hour_trend_not_positive")
    if not (-0.35 <= f.vwap_distance_atr <= 0.45):
        b.append("not_near_vwap")
    if f.rs_atr < 0.15:
        b.append("relative_strength_low")
    if f.volume_z < -0.5:
        b.append("volume_too_weak")
    if f.ret_15m_pct < -f.atr_pct * 0.65:
        b.append("pullback_too_deep")
    out["PULLBACK_RETEST"] = b

    return out
