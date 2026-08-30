from __future__ import annotations

import math
import os
import statistics
import pandas as pd


def _f(v, default=0.0):
    try:
        x = float(v)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _n(obj, *path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _signal_age_seconds(snapshot):
    base = snapshot.get("base_momentum") or {}
    raw = base.get("signal_time") or _n(base, "fast_features", "signal_time")
    if not raw:
        return None
    try:
        ts = pd.Timestamp(raw)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        snap = pd.Timestamp(int(snapshot.get("snapshot_ms") or 0), unit="ms", tz="UTC")
        return max(0.0, float((snap - ts).total_seconds()))
    except Exception:
        return None


def _base_ok(s):
    base = s.get("base_momentum") or {}
    if not base:
        return False
    ff = base.get("fast_features") or {}
    cs = base.get("cross_section") or {}
    age = _signal_age_seconds(s)
    checks = [
        age is not None and age <= float(os.getenv("V25_MAX_BASE_SIGNAL_AGE_SECONDS", "150")),
        _f(base.get("score")) >= float(os.getenv("V25_MIN_BASE_SCORE", "72")),
        _f(ff.get("ret_3m_pct")) > float(os.getenv("V25_MIN_RET_3M_PCT", "0.05")),
        _f(ff.get("rs_5m_pct")) >= float(os.getenv("V25_MIN_RS_5M_PCT", "0.10")),
        _f(ff.get("volume_acceleration")) >= float(os.getenv("V25_MIN_VOLUME_ACCEL", "1.15")),
        _f(ff.get("price_acceleration")) >= float(os.getenv("V25_MIN_PRICE_ACCEL", "-0.02")),
        _f(ff.get("current_move_pct")) <= float(os.getenv("V25_MAX_CURRENT_EXTENSION_PCT", "0.65")),
    ]
    if cs:
        checks += [
            _f(cs.get("composite_percentile")) >= float(os.getenv("V25_MIN_COMPOSITE_PERCENTILE", "0.72")),
            _f(cs.get("rs_5m_percentile")) >= float(os.getenv("V25_MIN_RS_PERCENTILE", "0.65")),
        ]
    return all(checks)


def _regime_ok(s):
    return not str(s.get("regime") or "").startswith("TREND_DOWN")


def _micro_ok(s):
    seq = s.get("sequence_context") or {}
    t5 = s.get("trade_5s") or {}
    depth = min(_f(s.get("bid_depth_5_usdt")), _f(s.get("ask_depth_5_usdt")))
    return all([
        _f(s.get("microstructure_score")) >= float(os.getenv("V25_MIN_MICRO_SCORE", "60")),
        _f(seq.get("score_persistence_3s")) >= float(os.getenv("V25_MIN_SCORE_PERSISTENCE_3S", "0.34")),
        _f(seq.get("buy_ratio_mean_3s"), 0.5) >= float(os.getenv("V25_MIN_BUY_RATIO_MEAN_3S", "0.52")),
        _f(seq.get("book_imbalance_mean_3s")) >= float(os.getenv("V25_MIN_BOOK_IMBALANCE_MEAN_3S", "-0.05")),
        _f(t5.get("buy_ratio"), 0.5) >= float(os.getenv("V25_MIN_BUY_RATIO_5S", "0.53")),
        _f(s.get("spread_pct"), 999) <= float(os.getenv("V25_MAX_SPREAD_PCT", "0.08")),
        depth >= float(os.getenv("V25_MIN_DEPTH_USDT", "500")),
    ])


def _cross_ok(s):
    cross = s.get("cross_exchange") or {}
    if not (cross.get("binance_available") or cross.get("okx_available")):
        return True
    return _f(cross.get("external_minus_bybit_move_5s_pct")) >= float(
        os.getenv("V25_MIN_EXTERNAL_LEAD_5S_PCT", "-0.08")
    )


def _perp_ok(s):
    perp = s.get("perp_context") or {}
    return _f(perp.get("oi_change_30s_pct")) >= float(
        os.getenv("V25_MIN_OI_CHANGE_30S_PCT", "-0.20")
    )



def _base_parts(s):
    base = s.get("base_momentum") or {}
    ff = base.get("fast_features") or {}
    cs = base.get("cross_section") or {}
    nm = base.get("normalized_momentum") or {}
    return base, ff, cs, nm


def _research_cross_section_momentum(s):
    base, ff, cs, nm = _base_parts(s)
    if not base or not cs:
        return False
    age = _signal_age_seconds(s)
    return all([
        age is not None and age <= 150.0,
        _f(cs.get("composite_percentile")) >= 0.80,
        _f(cs.get("rs_5m_percentile")) >= 0.75,
        _f(ff.get("ret_3m_pct")) > 0.0,
        _f(nm.get("rs_5m_over_rv")) >= 0.75,
        _f(ff.get("current_move_pct")) <= 0.80,
    ])


def _research_volume_breakout(s):
    base, ff, cs, nm = _base_parts(s)
    if not base or not cs:
        return False
    age = _signal_age_seconds(s)
    return all([
        age is not None and age <= 150.0,
        _f(cs.get("volume_accel_percentile")) >= 0.85,
        _f(cs.get("rs_5m_percentile")) >= 0.60,
        _f(ff.get("volume_acceleration")) >= 1.50,
        _f(ff.get("ret_3m_pct")) > 0.0,
        _f(ff.get("current_move_pct")) <= 0.90,
    ])


def _research_normalized_continuation(s):
    base, ff, cs, nm = _base_parts(s)
    if not base:
        return False
    age = _signal_age_seconds(s)
    return all([
        age is not None and age <= 150.0,
        _f(nm.get("ret_3m_over_rv")) >= 1.00,
        _f(nm.get("rs_5m_over_rv")) >= 0.75,
        _f(ff.get("ret_5m_pct")) > 0.0,
        _f(ff.get("price_acceleration")) >= -0.03,
        _f(ff.get("current_move_pct")) <= 0.80,
    ])


def _research_liquidation_reversal(s):
    perp = s.get("perp_context") or {}
    liq = perp.get("liquidation_5s") or {}
    seq = s.get("sequence_context") or {}
    long_liq = _f(liq.get("long_liq_notional"))
    depth = max(_f(s.get("bid_depth_5_usdt")) + _f(s.get("ask_depth_5_usdt")), 1.0)
    return all([
        long_liq >= 5000.0,
        long_liq / depth >= 0.25,
        _f(s.get("price_move_5s_pct")) <= -0.05,
        _f(seq.get("book_imbalance_mean_3s")) >= 0.0,
        _f(seq.get("buy_ratio_mean_3s"), 0.5) >= 0.52,
    ])


def _research_short_squeeze(s):
    base, ff, cs, nm = _base_parts(s)
    perp = s.get("perp_context") or {}
    liq = perp.get("liquidation_5s") or {}
    short_liq = _f(liq.get("short_liq_notional"))
    return all([
        bool(base),
        short_liq >= 5000.0,
        _f(ff.get("rs_5m_pct")) > 0.0,
        _f(ff.get("ret_3m_pct")) > 0.0,
        _f(nm.get("rs_5m_over_rv")) >= 0.50,
        _cross_ok(s),
    ])


RESEARCH_HYPOTHESES = {
    "cross_section_momentum": _research_cross_section_momentum,
    "volume_breakout": _research_volume_breakout,
    "normalized_continuation": _research_normalized_continuation,
    "liquidation_reversal": _research_liquidation_reversal,
    "short_squeeze_continuation": _research_short_squeeze,
}

VARIANTS = {
    "base_only": lambda s: _base_ok(s),
    "base_regime": lambda s: _base_ok(s) and _regime_ok(s),
    "base_micro": lambda s: _base_ok(s) and _micro_ok(s),
    "base_micro_cross": lambda s: _base_ok(s) and _micro_ok(s) and _cross_ok(s),
    "full_v25": lambda s: _base_ok(s) and _regime_ok(s) and _micro_ok(s) and _cross_ok(s) and _perp_ok(s),
}


def _nonoverlap(rows, horizon_seconds):
    out, last = [], {}
    gap = int(horizon_seconds) * 1000
    for r in sorted(rows, key=lambda x: int(x.get("snapshot_ms") or 0)):
        sym = str(r.get("symbol") or "")
        ts = int(r.get("snapshot_ms") or 0)
        prev = int(last.get(sym) or 0)
        if prev and ts - prev < gap:
            continue
        out.append(r)
        last[sym] = ts
    return out


def _split(rows, horizon_seconds):
    if len(rows) < 3:
        return rows, []
    ordered = sorted(rows, key=lambda x: int(x.get("snapshot_ms") or 0))
    idx = max(1, min(len(ordered)-1, int(len(ordered) * 0.70)))
    cut = int(ordered[idx].get("snapshot_ms") or 0)
    emb = int(horizon_seconds) * 1000
    return (
        [r for r in ordered if int(r.get("snapshot_ms") or 0) <= cut - emb],
        [r for r in ordered if int(r.get("snapshot_ms") or 0) >= cut + emb],
    )


def _max_drawdown_pct(returns_pct):
    equity = 100.0
    peak = 100.0
    worst = 0.0
    for r in returns_pct:
        equity *= 1.0 + float(r) / 100.0
        peak = max(peak, equity)
        if peak > 0:
            worst = min(worst, (equity / peak - 1.0) * 100.0)
    return worst


def _metrics(rows):
    if not rows:
        return {
            "n":0,
            "avg_gross_pct":None,
            "avg_spot_net_pct":None,
            "median_spot_net_pct":None,
            "spot_profit_factor":None,
            "spot_lower_mean_90_pct":None,
            "spot_max_drawdown_pct":None,
            "avg_perp1x_fee_cf_pct":None,
            "positive_spot_net_fraction":None,
        }

    spot_cost = 2.0 * float(os.getenv("V2_FEE_RATE", "0.001")) * 100.0 + float(os.getenv("V24_ENTRY_SLIPPAGE_PCT","0.03")) + float(os.getenv("V24_EXIT_SLIPPAGE_PCT","0.03"))
    perp_cost = 2.0 * float(os.getenv("V25_PERP_TAKER_FEE_RATE","0.00055")) * 100.0 + float(os.getenv("V24_ENTRY_SLIPPAGE_PCT","0.03")) + float(os.getenv("V24_EXIT_SLIPPAGE_PCT","0.03"))
    gross = [_f((r.get("label") or {}).get("final_bid_return_pct")) for r in rows]
    spot = [x - spot_cost for x in gross]
    perp = [x - perp_cost for x in gross]
    n=len(rows)

    avg_spot = sum(spot) / n
    med_spot = statistics.median(spot)
    wins = sum(x for x in spot if x > 0)
    losses = abs(sum(x for x in spot if x < 0))
    pf = None if losses <= 1e-12 else wins / losses

    lower90 = None
    if n >= 2:
        std = statistics.stdev(spot)
        lower90 = avg_spot - 1.6448536269514722 * std / math.sqrt(n)

    avg_gross = sum(gross) / n
    cost_sensitivity = {
        str(cost): avg_gross - float(cost)
        for cost in (0.10, 0.17, 0.26, 0.35)
    }

    return {
        "n":n,
        "avg_gross_pct":avg_gross,
        "cost_sensitivity_pct":cost_sensitivity,
        "avg_spot_net_pct":avg_spot,
        "median_spot_net_pct":med_spot,
        "spot_profit_factor":pf,
        "spot_lower_mean_90_pct":lower90,
        "spot_max_drawdown_pct":_max_drawdown_pct(spot),
        "avg_perp1x_fee_cf_pct":sum(perp)/n,
        "positive_spot_net_fraction":sum(x>0 for x in spot)/n,
        "positive_perp1x_cf_fraction":sum(x>0 for x in perp)/n,
    }


def run_v25_evidence(store):
    result={"engine":"MomentumAgentV2.5","mode":"LAYER_ABLATION_PURGED","auto_apply":False,"horizons":{},"hypotheses":{}}
    for horizon in (300,900,1800):
        raw=store.v24_labeled_snapshots(horizon, limit=30000)
        h={"raw_n":len(raw),"variants":{}}
        for name,gate in VARIANTS.items():
            selected=[r for r in raw if gate(r.get("snapshot") or {})]
            selected=_nonoverlap(selected,horizon)
            train,valid=_split(selected,horizon)
            h["variants"][name]={
                "effective_n":len(selected),
                "train":_metrics(train),
                "validation":_metrics(valid),
            }
        base_v=h["variants"]["base_only"]["validation"]
        for name,d in h["variants"].items():
            v=d["validation"]
            d["validation_spot_lift_vs_base_pct"]=None if v["avg_spot_net_pct"] is None or base_v["avg_spot_net_pct"] is None else v["avg_spot_net_pct"]-base_v["avg_spot_net_pct"]
        result["horizons"][str(horizon)]=h

        hyp = {}
        for name, gate in RESEARCH_HYPOTHESES.items():
            selected = [r for r in raw if gate(r.get("snapshot") or {})]
            selected = _nonoverlap(selected, horizon)
            train, valid = _split(selected, horizon)
            hyp[name] = {
                "effective_n": len(selected),
                "train": _metrics(train),
                "validation": _metrics(valid),
                "research_only": True,
            }
        result["hypotheses"][str(horizon)] = hyp

    promotion = {
        "candidate_promotable": False,
        "reasons": [],
        "required_validation_n": int(os.getenv("V25_PROMOTION_MIN_VALIDATION_N", "30")),
        "required_profit_factor": float(os.getenv("V25_PROMOTION_MIN_PROFIT_FACTOR", "1.20")),
        "max_drawdown_pct": float(os.getenv("V25_PROMOTION_MAX_DRAWDOWN_PCT", "10.0")),
        "required_horizons": [900, 1800],
        "requires_positive_lower_mean_90": True,
    }

    for horizon in promotion["required_horizons"]:
        h = result["horizons"].get(str(horizon)) or {}
        full = (h.get("variants") or {}).get("full_v25") or {}
        valid = full.get("validation") or {}
        n = int(valid.get("n") or 0)
        net = valid.get("avg_spot_net_pct")
        median = valid.get("median_spot_net_pct")
        lower90 = valid.get("spot_lower_mean_90_pct")
        pf = valid.get("spot_profit_factor")
        dd = abs(float(valid.get("spot_max_drawdown_pct") or 0.0))
        lift = full.get("validation_spot_lift_vs_base_pct")

        if n < promotion["required_validation_n"]:
            promotion["reasons"].append(f"h{horizon}:insufficient_validation_n")
        if net is None or float(net) <= 0.0:
            promotion["reasons"].append(f"h{horizon}:spot_net_nonpositive")
        if median is None or float(median) <= 0.0:
            promotion["reasons"].append(f"h{horizon}:median_spot_net_nonpositive")
        if lower90 is None or float(lower90) <= 0.0:
            promotion["reasons"].append(f"h{horizon}:lower_mean_90_nonpositive")
        if pf is None or float(pf) < promotion["required_profit_factor"]:
            promotion["reasons"].append(f"h{horizon}:profit_factor_low")
        if dd > promotion["max_drawdown_pct"]:
            promotion["reasons"].append(f"h{horizon}:drawdown_too_high")
        if lift is None or float(lift) <= 0.0:
            promotion["reasons"].append(f"h{horizon}:no_lift_vs_base")

    promotion["candidate_promotable"] = len(promotion["reasons"]) == 0
    result["promotion"] = promotion

    best = None
    for horizon, families in result["hypotheses"].items():
        for name, d in families.items():
            valid = d.get("validation") or {}
            n = int(valid.get("n") or 0)
            spot = valid.get("avg_spot_net_pct")
            perp = valid.get("avg_perp1x_fee_cf_pct")
            if n < int(os.getenv("V25_HYPOTHESIS_MIN_VALIDATION_N", "15")):
                continue
            candidate = {
                "horizon_seconds": int(horizon),
                "name": name,
                "validation_n": n,
                "spot_net_pct": spot,
                "perp1x_fee_cf_pct": perp,
            }
            key = float(perp if perp is not None else -999.0)
            if best is None or key > float(best.get("perp1x_fee_cf_pct") or -999.0):
                best = candidate
    result["best_validated_hypothesis"] = best
    store.set_runtime("v25_evidence",result)
    return result
