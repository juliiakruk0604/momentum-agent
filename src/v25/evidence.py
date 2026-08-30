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


def _research_efficient_trend_continuation(s):
    base, ff, cs, nm = _base_parts(s)
    if not base or not cs:
        return False
    age = _signal_age_seconds(s)
    return all([
        age is not None and age <= 150.0,
        _f(ff.get("ret_3m_pct")) > 0.0,
        _f(nm.get("ret_3m_over_rv")) >= 0.75,
        _f(nm.get("rs_5m_over_rv")) >= 0.50,
        _f(cs.get("trend_efficiency_30m_percentile")) >= 0.75,
        _f(cs.get("amihud_30m_percentile"), 1.0) <= 0.65,
        _f(ff.get("current_move_pct")) <= 0.80,
    ])


GEN2_HYPOTHESES = {
    "efficient_trend_continuation": _research_efficient_trend_continuation,
}


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
    # Keep the no-loss convention finite so the runtime JSON remains PostgreSQL-safe.
    pf = (999.0 if wins > 0 else None) if losses <= 1e-12 else wins / losses

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
        "spot_profit_factor_no_losses": losses <= 1e-12 and wins > 0,
        "spot_lower_mean_90_pct":lower90,
        "spot_max_drawdown_pct":_max_drawdown_pct(spot),
        "avg_perp1x_fee_cf_pct":sum(perp)/n,
        "positive_spot_net_fraction":sum(x>0 for x in spot)/n,
        "positive_perp1x_cf_fraction":sum(x>0 for x in perp)/n,
        "maker_entry_economics": {
            "status": "not_evaluated",
            "promotable": False,
            "reason": "passive_fill_probability_and_queue_position_not_labeled",
        },
    }


def _hypothesis_promotion(metrics, lift_vs_base):
    required_n = int(os.getenv("V25_HYPOTHESIS_MIN_VALIDATION_N", "15"))
    required_pf = float(os.getenv("V25_HYPOTHESIS_MIN_PROFIT_FACTOR", "1.20"))
    reasons = []
    n = int(metrics.get("n") or 0)
    net = metrics.get("avg_spot_net_pct")
    lower90 = metrics.get("spot_lower_mean_90_pct")
    pf = metrics.get("spot_profit_factor")

    if n < required_n:
        reasons.append("insufficient_validation_n")
    if net is None or float(net) <= 0.0:
        reasons.append("spot_net_nonpositive")
    if lift_vs_base is None or float(lift_vs_base) <= 0.0:
        reasons.append("no_lift_vs_base")
    if pf is None or float(pf) < required_pf:
        reasons.append("profit_factor_low")
    if lower90 is None or float(lower90) <= 0.0:
        reasons.append("lower_mean_90_nonpositive")

    return {
        "candidate_promotable": len(reasons) == 0,
        "reasons": reasons,
        "required_validation_n": required_n,
        "required_profit_factor": required_pf,
        "requires_positive_spot_net": True,
        "requires_positive_lift_vs_base": True,
        "requires_positive_lower_mean_90": True,
    }


def _fixed_context_diagnostics(raw, horizon):
    """Descriptive fixed-bin diagnostics. They never alter signal thresholds."""
    independent = _nonoverlap(raw, horizon)
    _, valid = _split(independent, horizon)
    deciles = {}
    vol_buckets = {
        "lt_0.15": [],
        "0.15_to_0.30": [],
        "0.30_to_0.60": [],
        "0.60_to_1.20": [],
        "gte_1.20": [],
    }


def _base_validation_metrics(rows, horizon):
    selected = [r for r in rows if _base_ok(r.get("snapshot") or {})]
    selected = _nonoverlap(selected, horizon)
    _, valid = _split(selected, horizon)
    return _metrics(valid)
    for row in valid:
        snapshot = row.get("snapshot") or {}
        _, ff, cs, _ = _base_parts(snapshot)
        rank = _f(cs.get("composite_percentile"), -1.0)
        if 0.0 <= rank <= 1.0:
            idx = min(9, int(rank * 10.0))
            deciles.setdefault(str(idx + 1), []).append(row)

        rv = _f(ff.get("realized_vol_20m_pct"), -1.0)
        if rv < 0.0:
            continue
        if rv < 0.15:
            key = "lt_0.15"
        elif rv < 0.30:
            key = "0.15_to_0.30"
        elif rv < 0.60:
            key = "0.30_to_0.60"
        elif rv < 1.20:
            key = "0.60_to_1.20"
        else:
            key = "gte_1.20"
        vol_buckets[key].append(row)

    return {
        "research_only": True,
        "auto_apply": False,
        "cross_section_composite_deciles": {
            key: _metrics(deciles.get(key, [])) for key in map(str, range(1, 11))
        },
        "realized_vol_20m_fixed_buckets_pct": {
            key: _metrics(rows) for key, rows in vol_buckets.items()
        },
    }


def _coverage_metrics(rows):
    n = len(rows)
    if n == 0:
        return {
            "n":0,
            "base_fraction":0.0,
            "cross_section_fraction":0.0,
            "normalized_fraction":0.0,
            "sequence_fraction":0.0,
            "perp_fraction":0.0,
            "cross_exchange_fraction":0.0,
        }
    def frac(fn):
        return sum(bool(fn(r.get("snapshot") or {})) for r in rows) / n
    return {
        "n": n,
        "base_fraction": frac(lambda s: s.get("base_momentum")),
        "cross_section_fraction": frac(lambda s: _n(s, "base_momentum", "cross_section")),
        "normalized_fraction": frac(lambda s: _n(s, "base_momentum", "normalized_momentum")),
        "sequence_fraction": frac(lambda s: s.get("sequence_context")),
        "perp_fraction": frac(lambda s: _n(s, "perp_context", "available")),
        "cross_exchange_fraction": frac(
            lambda s: bool(_n(s, "cross_exchange", "binance_available"))
            or bool(_n(s, "cross_exchange", "okx_available"))
        ),
    }


def run_v25_evidence(store):
    boundary_row = (
        store.get_runtime("v25_full_base_context_started")
        if hasattr(store, "get_runtime") else None
    )
    boundary = None if boundary_row is None else boundary_row.get("value")
    boundary_ms = int((boundary or {}).get("started_ms") or 0)
    gen2_row = (
        store.get_runtime("v25_slow_state_gen2_started")
        if hasattr(store, "get_runtime") else None
    )
    gen2_boundary = None if gen2_row is None else gen2_row.get("value")
    gen2_boundary_ms = int((gen2_boundary or {}).get("started_ms") or 0)
    result={
        "engine":"MomentumAgentV2.5",
        "mode":"LAYER_ABLATION_PURGED",
        "auto_apply":False,
        "generation_boundary":boundary,
        "gen2_boundary":gen2_boundary,
        "horizons":{},
        "hypotheses":{},
        "gen2_hypotheses":{},
        "context_diagnostics":{},
        "execution_economics": {
            "taker_labels": "current_best_ask_to_future_best_bid",
            "maker_model": "disabled_without_queue_and_fill_labels",
            "maker_results_promotable": False,
        },
    }
    for horizon in (300,900,1800):
        if hasattr(store, "v24_labeled_snapshots_with_base"):
            raw=store.v24_labeled_snapshots_with_base(
                horizon,
                limit=30000,
                max_base_age_seconds=int(os.getenv("V25_MAX_BASE_SIGNAL_AGE_SECONDS", "150")),
            )
        else:
            # Compatibility for isolated stores and older research fixtures.
            raw=store.v24_labeled_snapshots(horizon, limit=30000)
        post_boundary = [
            r for r in raw
            if boundary_ms <= 0 or int(r.get("snapshot_ms") or 0) >= boundary_ms
        ]
        h={
            "raw_n":len(raw),
            "post_boundary_n":len(post_boundary),
            "coverage_all":_coverage_metrics(raw),
            "coverage_post_boundary":_coverage_metrics(post_boundary),
            "variants":{},
        }
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
        result["context_diagnostics"][str(horizon)] = _fixed_context_diagnostics(raw, horizon)

        hyp = {}
        for name, gate in RESEARCH_HYPOTHESES.items():
            source_rows = post_boundary if name in {
                "cross_section_momentum",
                "volume_breakout",
            } else raw
            selected = [r for r in source_rows if gate(r.get("snapshot") or {})]
            selected = _nonoverlap(selected, horizon)
            train, valid = _split(selected, horizon)
            hyp[name] = {
                "effective_n": len(selected),
                "train": _metrics(train),
                "validation": _metrics(valid),
                "research_only": True,
            }
            base_v = _base_validation_metrics(source_rows, horizon)
            valid_spot = hyp[name]["validation"].get("avg_spot_net_pct")
            base_spot = base_v.get("avg_spot_net_pct")
            lift = None if valid_spot is None or base_spot is None else valid_spot - base_spot
            hyp[name]["base_validation_benchmark"] = base_v
            hyp[name]["validation_spot_lift_vs_base_pct"] = lift
            hyp[name]["promotion"] = _hypothesis_promotion(hyp[name]["validation"], lift)
        result["hypotheses"][str(horizon)] = hyp

        gen2 = {}
        gen2_rows = [
            r for r in raw
            if gen2_boundary_ms > 0 and int(r.get("snapshot_ms") or 0) >= gen2_boundary_ms
        ]
        gen2_base_v = _base_validation_metrics(gen2_rows, horizon)
        for name, gate in GEN2_HYPOTHESES.items():
            selected = [r for r in gen2_rows if gate(r.get("snapshot") or {})]
            selected = _nonoverlap(selected, horizon)
            train, valid = _split(selected, horizon)
            gen2[name] = {
                "effective_n": len(selected),
                "train": _metrics(train),
                "validation": _metrics(valid),
                "research_only": True,
                "hypothesis_generation": 2,
            }
            valid_spot = gen2[name]["validation"].get("avg_spot_net_pct")
            base_spot = gen2_base_v.get("avg_spot_net_pct")
            lift = None if valid_spot is None or base_spot is None else valid_spot - base_spot
            gen2[name]["base_validation_benchmark"] = gen2_base_v
            gen2[name]["validation_spot_lift_vs_base_pct"] = lift
            gen2[name]["promotion"] = _hypothesis_promotion(gen2[name]["validation"], lift)
        result["gen2_hypotheses"][str(horizon)] = gen2

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
    hypothesis_groups = (
        (1, result["hypotheses"]),
        (2, result["gen2_hypotheses"]),
    )
    for generation, by_horizon in hypothesis_groups:
        for horizon, families in by_horizon.items():
          for name, d in families.items():
            valid = d.get("validation") or {}
            family_promotion = d.get("promotion") or {}
            if not bool(family_promotion.get("candidate_promotable")):
                continue
            n = int(valid.get("n") or 0)
            spot = valid.get("avg_spot_net_pct")
            perp = valid.get("avg_perp1x_fee_cf_pct")
            candidate = {
                "horizon_seconds": int(horizon),
                "name": name,
                "validation_n": n,
                "spot_net_pct": spot,
                "perp1x_fee_cf_pct": perp,
                "spot_lower_mean_90_pct": valid.get("spot_lower_mean_90_pct"),
                "spot_profit_factor": valid.get("spot_profit_factor"),
                "spot_lift_vs_base_pct": d.get("validation_spot_lift_vs_base_pct"),
                "hypothesis_generation": generation,
            }
            key = float(valid.get("spot_lower_mean_90_pct") or -999.0)
            if best is None or key > float(best.get("spot_lower_mean_90_pct") or -999.0):
                best = candidate
    result["best_validated_hypothesis"] = best
    store.set_runtime("v25_evidence",result)
    return result
