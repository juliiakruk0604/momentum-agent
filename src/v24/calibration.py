from __future__ import annotations

import math
import os
import pandas as pd


def _nested(obj, *path):
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


FEATURES = {
    # Bybit spot microstructure.
    "microstructure_score": ("microstructure", "high", lambda s: s.get("microstructure_score")),
    "microprice_edge_bps": ("microstructure", "high", lambda s: s.get("microprice_edge_bps")),
    "book_imbalance_5": ("microstructure", "high", lambda s: s.get("book_imbalance_5")),
    "book_imbalance_delta_1s": ("microstructure", "high", lambda s: s.get("book_imbalance_delta_1s")),
    "ask_depletion_1s": ("microstructure", "high", lambda s: s.get("ask_depletion_1s")),
    "bid_replenishment_1s": ("microstructure", "high", lambda s: s.get("bid_replenishment_1s")),
    "flow_confidence_1s": ("microstructure", "high", lambda s: s.get("flow_confidence_1s")),
    "flow_confidence_5s": ("microstructure", "high", lambda s: s.get("flow_confidence_5s")),
    "buy_ratio_1s": ("microstructure", "high", lambda s: _nested(s, "trade_1s", "buy_ratio")),
    "buy_ratio_5s": ("microstructure", "high", lambda s: _nested(s, "trade_5s", "buy_ratio")),
    "cvd_ratio_1s": ("microstructure", "high", lambda s: _nested(s, "trade_1s", "cvd_ratio")),
    "cvd_ratio_5s": ("microstructure", "high", lambda s: _nested(s, "trade_5s", "cvd_ratio")),
    "trade_notional_1s": ("microstructure", "high", lambda s: _nested(s, "trade_1s", "total_notional")),
    "price_move_1s_pct": ("microstructure", "high", lambda s: s.get("price_move_1s_pct")),
    "price_move_5s_pct": ("microstructure", "high", lambda s: s.get("price_move_5s_pct")),
    "spread_pct": ("microstructure", "low", lambda s: s.get("spread_pct")),

    # Bybit linear/perp context. Directional liquidation fields remain evidence-only.
    "oi_change_5s_pct": ("perp", "high", lambda s: _nested(s, "perp_context", "oi_change_5s_pct")),
    "oi_change_30s_pct": ("perp", "high", lambda s: _nested(s, "perp_context", "oi_change_30s_pct")),
    "perp_price_change_5s_pct": ("perp", "high", lambda s: _nested(s, "perp_context", "perp_price_change_5s_pct")),
    "perp_price_change_30s_pct": ("perp", "high", lambda s: _nested(s, "perp_context", "perp_price_change_30s_pct")),
    "abs_funding_rate": ("perp", "low", lambda s: abs(float(_nested(s, "perp_context", "funding_rate") or 0.0))),
    "liq_notional_5s": (
        "perp",
        "high",
        lambda s: float(_nested(s, "perp_context", "liquidation_5s", "long_liq_notional") or 0.0)
        + float(_nested(s, "perp_context", "liquidation_5s", "short_liq_notional") or 0.0),
    ),
    "liq_notional_30s": (
        "perp",
        "high",
        lambda s: float(_nested(s, "perp_context", "liquidation_30s", "long_liq_notional") or 0.0)
        + float(_nested(s, "perp_context", "liquidation_30s", "short_liq_notional") or 0.0),
    ),

    # External venue lead/lag. No feature here is authorized for trading by default.
    "external_consensus_move_1s_pct": ("cross_exchange", "high", lambda s: _nested(s, "cross_exchange", "external_consensus_move_1s_pct")),
    "external_consensus_move_5s_pct": ("cross_exchange", "high", lambda s: _nested(s, "cross_exchange", "external_consensus_move_5s_pct")),
    "external_minus_bybit_move_1s_pct": ("cross_exchange", "high", lambda s: _nested(s, "cross_exchange", "external_minus_bybit_move_1s_pct")),
    "external_minus_bybit_move_5s_pct": ("cross_exchange", "high", lambda s: _nested(s, "cross_exchange", "external_minus_bybit_move_5s_pct")),
    "binance_buy_ratio_1s": ("cross_exchange", "high", lambda s: _nested(s, "cross_exchange", "binance_trade_1s", "buy_ratio")),
    "binance_buy_ratio_5s": ("cross_exchange", "high", lambda s: _nested(s, "cross_exchange", "binance_trade_5s", "buy_ratio")),
    "okx_buy_ratio_1s": ("cross_exchange", "high", lambda s: _nested(s, "cross_exchange", "okx_trade_1s", "buy_ratio")),
    "okx_buy_ratio_5s": ("cross_exchange", "high", lambda s: _nested(s, "cross_exchange", "okx_trade_5s", "buy_ratio")),
}


def _f(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _roundtrip_cost_pct():
    fee_rate = float(os.getenv("V2_FEE_RATE", "0.001"))
    entry_slip = float(os.getenv("V24_ENTRY_SLIPPAGE_PCT", "0.03"))
    exit_slip = float(os.getenv("V24_EXIT_SLIPPAGE_PCT", "0.03"))
    # Entry labels already use ask and exits use bid, so spread is already embedded.
    return 2.0 * fee_rate * 100.0 + entry_slip + exit_slip


def _wilson_lower(hits, n, z=1.6448536269514722):
    if n <= 0:
        return 0.0
    p = float(hits) / float(n)
    denom = 1.0 + z * z / n
    centre = p + z * z / (2.0 * n)
    margin = z * math.sqrt((p * (1.0 - p) + z * z / (4.0 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _metrics(rows):
    if not rows:
        return {
            "n": 0,
            "avg_final_bid_return_pct": None,
            "avg_net_after_cost_pct": None,
            "avg_mfe_bid_pct": None,
            "avg_mae_bid_pct": None,
            "p_hit_0_1": None,
            "p_hit_0_25": None,
            "p_hit_0_5": None,
            "p_positive_final": None,
        }

    labels = [r["label"] for r in rows]
    n = len(labels)
    cost = _roundtrip_cost_pct()
    finals = [float(x.get("final_bid_return_pct") or 0.0) for x in labels]
    mfes = [float(x.get("mfe_bid_pct") or 0.0) for x in labels]
    maes = [float(x.get("mae_bid_pct") or 0.0) for x in labels]
    h01 = sum(bool(x.get("hit_0_1")) for x in labels)
    h025 = sum(bool(x.get("hit_0_25")) for x in labels)
    h05 = sum(bool(x.get("hit_0_5")) for x in labels)
    positive = sum(v > 0.0 for v in finals)
    net = [v - cost for v in finals]

    return {
        "n": n,
        "roundtrip_cost_pct": cost,
        "avg_final_bid_return_pct": sum(finals) / n,
        "avg_net_after_cost_pct": sum(net) / n,
        "avg_mfe_bid_pct": sum(mfes) / n,
        "avg_mae_bid_pct": sum(maes) / n,
        "p_hit_0_1": h01 / n,
        "p_hit_0_25": h025 / n,
        "p_hit_0_5": h05 / n,
        "p_positive_final": positive / n,
        "p_hit_0_1_wilson_lower_90": _wilson_lower(h01, n),
        "p_hit_0_25_wilson_lower_90": _wilson_lower(h025, n),
    }


def _quantile(values, q):
    return float(pd.Series(values, dtype="float64").quantile(q))


def _candidate_thresholds(values, direction):
    if len(values) < 30:
        return []
    quantiles = [0.50, 0.65, 0.75, 0.85, 0.90]
    if direction == "low":
        quantiles = [0.50, 0.35, 0.25, 0.15, 0.10]
    out = []
    seen = set()
    for q in quantiles:
        t = _quantile(values, q)
        key = round(t, 12)
        if key not in seen:
            seen.add(key)
            out.append(t)
    return out


def _subset(rows, getter, direction, threshold):
    out = []
    for row in rows:
        value = _f(getter(row["snapshot"]))
        if value is None:
            continue
        if direction == "high" and value >= threshold:
            out.append(row)
        elif direction == "low" and value <= threshold:
            out.append(row)
    return out


def _evaluate_rule(train_m, valid_m, base_train, base_valid):
    min_train = int(os.getenv("V24_CAL_MIN_TRAIN_SUBSET", "30"))
    min_valid = int(os.getenv("V24_CAL_MIN_VALID_SUBSET", "15"))
    if train_m["n"] < min_train or valid_m["n"] < min_valid:
        return None

    train_net = float(train_m.get("avg_net_after_cost_pct") or 0.0)
    valid_net = float(valid_m.get("avg_net_after_cost_pct") or 0.0)
    base_train_net = float(base_train.get("avg_net_after_cost_pct") or 0.0)
    base_valid_net = float(base_valid.get("avg_net_after_cost_pct") or 0.0)
    train_lift = train_net - base_train_net
    valid_lift = valid_net - base_valid_net

    train_hit = float(train_m.get("p_hit_0_1") or 0.0)
    valid_hit = float(valid_m.get("p_hit_0_1") or 0.0)
    base_train_hit = float(base_train.get("p_hit_0_1") or 0.0)
    base_valid_hit = float(base_valid.get("p_hit_0_1") or 0.0)

    robust = (
        train_lift > 0.0
        and valid_lift > 0.0
        and train_net > 0.0
        and valid_net > 0.0
        and float(train_m.get("avg_mfe_bid_pct") or 0.0) > float(base_train.get("avg_mfe_bid_pct") or 0.0)
        and float(valid_m.get("avg_mfe_bid_pct") or 0.0) > float(base_valid.get("avg_mfe_bid_pct") or 0.0)
        and train_hit >= base_train_hit
        and valid_hit >= base_valid_hit
    )

    score = (
        max(valid_net, -1.0) * 4.0
        + max(train_net, -1.0) * 2.0
        + max(valid_lift, 0.0) * 2.0
        + max(train_lift, 0.0)
        + max(valid_hit - base_valid_hit, 0.0)
    )

    return {
        "robust": robust,
        "score": round(score, 8),
        "train_net_lift_pct": round(train_lift, 8),
        "validation_net_lift_pct": round(valid_lift, 8),
    }


def calibrate_horizon(store, horizon_seconds):
    rows = store.v24_labeled_snapshots(int(horizon_seconds), limit=20000)
    n = len(rows)
    min_total = int(os.getenv("V24_CAL_MIN_ROWS", "200"))
    if n < min_total:
        return {
            "status": "collecting",
            "horizon_seconds": int(horizon_seconds),
            "n": n,
            "minimum_required": min_total,
            "baseline": _metrics(rows),
            "groups": {},
            "auto_apply": False,
        }

    split = max(1, int(n * float(os.getenv("V24_CAL_TRAIN_FRACTION", "0.70"))))
    split = min(split, n - 1)
    train = rows[:split]
    valid = rows[split:]
    base_train = _metrics(train)
    base_valid = _metrics(valid)

    rules = []
    for feature, (group, direction, getter) in FEATURES.items():
        values = [_f(getter(r["snapshot"])) for r in train]
        values = [v for v in values if v is not None]
        for threshold in _candidate_thresholds(values, direction):
            tm = _metrics(_subset(train, getter, direction, threshold))
            vm = _metrics(_subset(valid, getter, direction, threshold))
            evidence = _evaluate_rule(tm, vm, base_train, base_valid)
            if evidence is None:
                continue
            rules.append({
                "feature": feature,
                "group": group,
                "direction": direction,
                "threshold": threshold,
                "train": tm,
                "validation": vm,
                **evidence,
            })

    rules.sort(key=lambda x: (bool(x["robust"]), float(x["score"])), reverse=True)

    grouped = {}
    for group in ("microstructure", "perp", "cross_exchange"):
        group_rules = [r for r in rules if r["group"] == group]
        robust = [r for r in group_rules if r["robust"]]
        robust_by_feature = {}
        for r in robust:
            robust_by_feature[r["feature"]] = robust_by_feature.get(r["feature"], 0) + 1
        stable_features = sorted([k for k, count in robust_by_feature.items() if count >= 2])
        stable_rules = [r for r in robust if r["feature"] in stable_features]
        grouped[group] = {
            "evaluated_rules": len(group_rules),
            "robust_rules": len(robust),
            "stable_features": stable_features,
            "stable_rule_count": len(stable_rules),
            "top_stable_rules": stable_rules[:8],
            "authorized_for_signal": False,
        }

    return {
        "status": "evaluated",
        "horizon_seconds": int(horizon_seconds),
        "n": n,
        "train_n": len(train),
        "validation_n": len(valid),
        "baseline_train": base_train,
        "baseline_validation": base_valid,
        "groups": grouped,
        "top_rules": rules[:20],
        "auto_apply": False,
    }


def run_v24_calibration(store):
    result = {
        "engine": "MomentumAgentV2.4",
        "mode": "EVENT_FEATURE_ABLATION",
        "label_version": "price_tick_v2",
        "auto_apply": False,
        "horizons": {},
    }
    for horizon in (5, 15, 30, 60):
        result["horizons"][str(horizon)] = calibrate_horizon(store, horizon)
    store.set_runtime("v24_empirical_calibration", result)
    return result
