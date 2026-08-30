from __future__ import annotations

from .models import SetupCandidate


def _candidate(f, setup, score, regime, reasons, stop_atr, reward_r):
    stop_pct = max(0.8, min(2.5, f.atr_pct * stop_atr))
    target_pct = max(1.6, min(6.0, stop_pct * reward_r))
    return SetupCandidate(
        symbol=f.symbol,
        setup=setup,
        score=round(max(0.0, min(100.0, score)), 2),
        signal_price=f.price,
        stop_pct=round(stop_pct, 4),
        target_pct=round(target_pct, 4),
        regime=regime.name,
        reasons=reasons,
        features=f.to_dict(),
    )


def momentum_breakout(f, regime):
    if regime.name.startswith("TREND_DOWN"):
        return None
    reasons = []
    if f.breakout_atr < 0.35:
        return None
    reasons.append("atr_normalized_breakout")
    if f.volume_z < 1.8:
        return None
    reasons.append("volume_z_expansion")
    if f.rs_atr < 0.35:
        return None
    reasons.append("relative_strength")
    if f.vwap_distance_atr < 0.1:
        return None
    reasons.append("above_vwap")
    if f.vwap_distance_atr > 2.2:
        return None
    reasons.append("not_overextended")

    score = (
        22 * min(f.breakout_atr / 1.2, 1.0)
        + 28 * min(max(f.volume_z, 0.0) / 4.0, 1.0)
        + 25 * min(max(f.rs_atr, 0.0) / 1.5, 1.0)
        + 15 * min(max(f.bb_width_expansion - 0.8, 0.0) / 0.8, 1.0)
        + (10 if regime.name.startswith("TREND_UP") else 4)
    )
    return _candidate(f, "MOMENTUM_BREAKOUT", score, regime, reasons, 1.25, 2.2)


def early_acceleration(f, regime):
    if regime.name.startswith("TREND_DOWN") or regime.name == "CHOP":
        return None
    if not (0.05 <= f.breakout_atr <= 0.55):
        return None
    if f.volume_z < 1.4 or f.rs_atr < 0.25:
        return None
    if f.ret_15m_pct <= 0 or f.bb_width_expansion < 1.0:
        return None
    if f.vwap_distance_atr < 0 or f.vwap_distance_atr > 1.6:
        return None
    score = (
        25 * min(f.breakout_atr / 0.55, 1.0)
        + 25 * min(max(f.volume_z, 0.0) / 3.5, 1.0)
        + 25 * min(max(f.rs_atr, 0.0) / 1.2, 1.0)
        + 15 * min(max(f.bb_width_expansion - 0.9, 0.0) / 0.6, 1.0)
        + 10
    )
    return _candidate(
        f, "EARLY_ACCELERATION", score, regime,
        ["pre_breakout_acceleration","volume_z_expansion","relative_strength","volatility_expansion"],
        1.15, 2.4
    )


def pullback_retest(f, regime):
    if not regime.name.startswith("TREND_UP"):
        return None
    # A valid retest sits near VWAP after a positive 4h move, while relative strength survives.
    if f.ret_4h_pct <= 0:
        return None
    if not (-0.35 <= f.vwap_distance_atr <= 0.45):
        return None
    if f.rs_atr < 0.15 or f.volume_z < -0.5:
        return None
    if f.ret_15m_pct < -f.atr_pct * 0.65:
        return None
    score = (
        25 * min(max(f.ret_4h_pct, 0.0) / max(f.atr_pct * 5.0, 1e-6), 1.0)
        + 25 * min(max(f.rs_atr, 0.0) / 1.0, 1.0)
        + 20 * (1.0 - min(abs(f.vwap_distance_atr) / 0.45, 1.0))
        + 15 * min(max(f.volume_z + 0.5, 0.0) / 2.5, 1.0)
        + 15
    )
    return _candidate(
        f, "PULLBACK_RETEST", score, regime,
        ["trend_pullback","near_vwap","relative_strength_survives"],
        1.05, 2.6
    )


def evaluate_setups(f, regime):
    out = []
    for fn in (momentum_breakout, early_acceleration, pullback_retest):
        c = fn(f, regime)
        if c is not None:
            out.append(c)
    return out
