from __future__ import annotations

import math
import os
import statistics

import pandas as pd


FEATURES = {
    "score": ("high", lambda s: s.get("score")),
    "coarse_score": ("high", lambda s: (s.get("fast_features") or {}).get("coarse_score")),
    "volume_acceleration": ("high", lambda s: (s.get("fast_features") or {}).get("volume_acceleration")),
    "price_acceleration": ("high", lambda s: (s.get("fast_features") or {}).get("price_acceleration")),
    "current_move_pct": ("high", lambda s: (s.get("fast_features") or {}).get("current_move_pct")),
    "rs_5m_pct": ("high", lambda s: (s.get("fast_features") or {}).get("rs_5m_pct")),
    "flow_score": ("high", lambda s: s.get("flow_score")),
    "flow_confidence": ("high", lambda s: (s.get("trade_flow") or {}).get("flow_confidence")),
    "recent_buy_ratio": ("high", lambda s: (s.get("trade_flow") or {}).get("recent_buy_ratio")),
    "recent_notional": ("high", lambda s: (s.get("trade_flow") or {}).get("recent_notional")),
    "book_imbalance": ("high", lambda s: (s.get("book") or {}).get("book_imbalance")),
    "depth_usdt": ("high", lambda s: (s.get("book") or {}).get("depth_usdt")),
    "spread_pct": ("low", lambda s: (s.get("book") or {}).get("spread_pct")),
}


def _f(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _metrics(rows):
    if not rows:
        return {
            "n": 0,
            "avg_final_return_pct": None,
            "avg_mfe_pct": None,
            "avg_mae_pct": None,
            "p_hit_0_5": None,
            "p_hit_1": None,
            "p_hit_2": None,
        }
    labels = [r["label"] for r in rows]
    n = len(labels)
    return {
        "n": n,
        "avg_final_return_pct": sum(float(x.get("final_return_pct") or 0.0) for x in labels) / n,
        "avg_mfe_pct": sum(float(x.get("mfe_pct") or 0.0) for x in labels) / n,
        "avg_mae_pct": sum(float(x.get("mae_pct") or 0.0) for x in labels) / n,
        "p_hit_0_5": sum(bool(x.get("hit_0_5")) for x in labels) / n,
        "p_hit_1": sum(bool(x.get("hit_1")) for x in labels) / n,
        "p_hit_2": sum(bool(x.get("hit_2")) for x in labels) / n,
    }


def _quantile(values, q):
    s = pd.Series(values, dtype="float64")
    return float(s.quantile(q))


def _candidate_thresholds(values, direction):
    if len(values) < 20:
        return []
    qs = [0.50, 0.65, 0.75, 0.85, 0.90]
    if direction == "low":
        qs = [0.50, 0.35, 0.25, 0.15, 0.10]
    out = []
    seen = set()
    for q in qs:
        t = _quantile(values, q)
        key = round(t, 12)
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _subset(rows, getter, direction, threshold):
    out = []
    for r in rows:
        value = _f(getter(r["snapshot"]))
        if value is None:
            continue
        if direction == "high" and value >= threshold:
            out.append(r)
        elif direction == "low" and value <= threshold:
            out.append(r)
    return out


def _score_rule(train_metrics, valid_metrics, base_train, base_valid):
    if train_metrics["n"] < 10 or valid_metrics["n"] < 8:
        return None
    bt = float(base_train.get("p_hit_0_5") or 0.0)
    bv = float(base_valid.get("p_hit_0_5") or 0.0)
    tt = float(train_metrics.get("p_hit_0_5") or 0.0)
    tv = float(valid_metrics.get("p_hit_0_5") or 0.0)
    mfe_t = float(train_metrics.get("avg_mfe_pct") or 0.0)
    mfe_v = float(valid_metrics.get("avg_mfe_pct") or 0.0)
    ret_t = float(train_metrics.get("avg_final_return_pct") or 0.0)
    ret_v = float(valid_metrics.get("avg_final_return_pct") or 0.0)

    train_lift = tt - bt
    valid_lift = tv - bv
    robust = train_lift > 0 and valid_lift > 0 and mfe_t > 0 and mfe_v > 0
    score = valid_lift * 3.0 + train_lift + max(mfe_v, 0.0) + max(ret_v, 0.0) * 0.5
    return {
        "robust": robust,
        "score": round(score, 6),
        "train_hit_0_5_lift": round(train_lift, 6),
        "valid_hit_0_5_lift": round(valid_lift, 6),
    }


def calibrate_horizon(store, horizon_minutes=5):
    rows = store.v22_labeled_snapshots(int(horizon_minutes), limit=10000)
    n = len(rows)
    min_total = int(os.getenv("V22_CALIBRATION_MIN_ROWS", "120"))
    if n < min_total:
        return {
            "horizon_minutes": int(horizon_minutes),
            "status": "collecting",
            "n": n,
            "minimum_required": min_total,
            "baseline": _metrics(rows),
            "rules": [],
        }

    split = max(1, int(n * float(os.getenv("V22_CALIBRATION_TRAIN_FRACTION", "0.70"))))
    split = min(split, n - 1)
    train = rows[:split]
    valid = rows[split:]
    base_train = _metrics(train)
    base_valid = _metrics(valid)

    rules = []
    for name, (direction, getter) in FEATURES.items():
        values = [_f(getter(r["snapshot"])) for r in train]
        values = [x for x in values if x is not None]
        for threshold in _candidate_thresholds(values, direction):
            train_subset = _subset(train, getter, direction, threshold)
            valid_subset = _subset(valid, getter, direction, threshold)
            tm = _metrics(train_subset)
            vm = _metrics(valid_subset)
            scoring = _score_rule(tm, vm, base_train, base_valid)
            if scoring is None:
                continue
            rules.append({
                "feature": name,
                "direction": direction,
                "threshold": threshold,
                "train": tm,
                "validation": vm,
                **scoring,
            })

    rules.sort(key=lambda r: (bool(r["robust"]), float(r["score"])), reverse=True)
    robust = [r for r in rules if r["robust"]]

    return {
        "horizon_minutes": int(horizon_minutes),
        "status": "evaluated",
        "n": n,
        "train_n": len(train),
        "validation_n": len(valid),
        "baseline_train": base_train,
        "baseline_validation": base_valid,
        "robust_rule_count": len(robust),
        "rules": rules[:30],
        "recommended_single_feature_rules": robust[:8],
        "auto_apply": False,
    }


def run_calibration(store):
    horizons = (5, 15, 30, 60)
    result = {
        "engine": "MomentumAgentV2.2",
        "mode": "FORWARD_EMPIRICAL_CALIBRATION",
        "auto_apply": False,
        "horizons": {},
    }
    for h in horizons:
        result["horizons"][str(h)] = calibrate_horizon(store, h)
    store.set_runtime("v22_empirical_calibration", result)
    return result
