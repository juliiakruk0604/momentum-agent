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

    # Multi-second temporal persistence. Evidence-only until validated.
    "score_delta_1s": ("sequence", "high", lambda s: _nested(s, "sequence_context", "score_delta_1s")),
    "score_delta_3s": ("sequence", "high", lambda s: _nested(s, "sequence_context", "score_delta_3s")),
    "score_mean_3s": ("sequence", "high", lambda s: _nested(s, "sequence_context", "score_mean_3s")),
    "score_min_3s": ("sequence", "high", lambda s: _nested(s, "sequence_context", "score_min_3s")),
    "score_persistence_3s": ("sequence", "high", lambda s: _nested(s, "sequence_context", "score_persistence_3s")),
    "buy_ratio_mean_3s": ("sequence", "high", lambda s: _nested(s, "sequence_context", "buy_ratio_mean_3s")),
    "buy_persistence_3s": ("sequence", "high", lambda s: _nested(s, "sequence_context", "buy_persistence_3s")),
    "book_imbalance_mean_3s": ("sequence", "high", lambda s: _nested(s, "sequence_context", "book_imbalance_mean_3s")),
    "ask_depletion_mean_3s": ("sequence", "high", lambda s: _nested(s, "sequence_context", "ask_depletion_mean_3s")),
    "external_lead_mean_3s": ("sequence", "high", lambda s: _nested(s, "sequence_context", "external_lead_mean_3s")),
    "external_positive_lead_persistence_3s": ("sequence", "high", lambda s: _nested(s, "sequence_context", "external_positive_lead_persistence_3s")),
    "cross_exchange_agreement_persistence_3s": ("sequence", "high", lambda s: _nested(s, "sequence_context", "cross_exchange_agreement_persistence_3s")),
    "oi_change_mean_5s_pct": ("sequence", "high", lambda s: _nested(s, "sequence_context", "oi_change_mean_5s_pct")),

    # Primary alpha candidate from the slower V2.2 momentum engine.
    "base_score": ("base_momentum", "high", lambda s: _nested(s, "base_momentum", "score")),
    "base_ret_3m_pct": ("base_momentum", "high", lambda s: _nested(s, "base_momentum", "fast_features", "ret_3m_pct")),
    "base_ret_5m_pct": ("base_momentum", "high", lambda s: _nested(s, "base_momentum", "fast_features", "ret_5m_pct")),
    "base_rs_5m_pct": ("base_momentum", "high", lambda s: _nested(s, "base_momentum", "fast_features", "rs_5m_pct")),
    "base_volume_acceleration": ("base_momentum", "high", lambda s: _nested(s, "base_momentum", "fast_features", "volume_acceleration")),
    "base_price_acceleration": ("base_momentum", "high", lambda s: _nested(s, "base_momentum", "fast_features", "price_acceleration")),
    "base_current_move_pct": ("base_momentum", "high", lambda s: _nested(s, "base_momentum", "fast_features", "current_move_pct")),
    "base_rs_percentile": ("base_momentum", "high", lambda s: _nested(s, "base_momentum", "cross_section", "rs_5m_percentile")),
    "base_ret3_percentile": ("base_momentum", "high", lambda s: _nested(s, "base_momentum", "cross_section", "ret_3m_percentile")),
    "base_volume_percentile": ("base_momentum", "high", lambda s: _nested(s, "base_momentum", "cross_section", "volume_accel_percentile")),
    "base_composite_percentile": ("base_momentum", "high", lambda s: _nested(s, "base_momentum", "cross_section", "composite_percentile")),
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
    tp035 = sum(bool(_nested(x, "passage_035_015", "tp_before_sl")) for x in labels)
    tp050 = sum(bool(_nested(x, "passage_050_025", "tp_before_sl")) for x in labels)
    tp100 = sum(bool(_nested(x, "passage_100_050", "tp_before_sl")) for x in labels)
    tp200 = sum(bool(_nested(x, "passage_200_075", "tp_before_sl")) for x in labels)

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
        "p_tp035_before_sl015": tp035 / n,
        "p_tp050_before_sl025": tp050 / n,
        "p_tp100_before_sl050": tp100 / n,
        "p_tp200_before_sl075": tp200 / n,
        "p_tp035_before_sl015_wilson_lower_90": _wilson_lower(tp035, n),
        "p_tp050_before_sl025_wilson_lower_90": _wilson_lower(tp050, n),
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

    target_train = float(train_m.get("p_tp035_before_sl015") or 0.0)
    target_valid = float(valid_m.get("p_tp035_before_sl015") or 0.0)
    target_base_train = float(base_train.get("p_tp035_before_sl015") or 0.0)
    target_base_valid = float(base_valid.get("p_tp035_before_sl015") or 0.0)
    target_train_lower = float(train_m.get("p_tp035_before_sl015_wilson_lower_90") or 0.0)
    target_valid_lower = float(valid_m.get("p_tp035_before_sl015_wilson_lower_90") or 0.0)

    robust = (
        train_lift > 0.0
        and valid_lift > 0.0
        and train_net > 0.0
        and valid_net > 0.0
        and float(train_m.get("avg_mfe_bid_pct") or 0.0) > float(base_train.get("avg_mfe_bid_pct") or 0.0)
        and float(valid_m.get("avg_mfe_bid_pct") or 0.0) > float(base_valid.get("avg_mfe_bid_pct") or 0.0)
        and train_hit >= base_train_hit
        and valid_hit >= base_valid_hit
        and target_train > target_base_train
        and target_valid > target_base_valid
        and target_train_lower > target_base_train
        and target_valid_lower > target_base_valid
    )

    score = (
        max(valid_net, -1.0) * 4.0
        + max(train_net, -1.0) * 2.0
        + max(valid_lift, 0.0) * 2.0
        + max(train_lift, 0.0)
        + max(valid_hit - base_valid_hit, 0.0)
        + max(target_valid - target_base_valid, 0.0) * 2.0
        + max(target_train - target_base_train, 0.0)
    )

    return {
        "robust": robust,
        "score": round(score, 8),
        "train_net_lift_pct": round(train_lift, 8),
        "validation_net_lift_pct": round(valid_lift, 8),
        "train_tp035_before_sl015_lift": round(target_train - target_base_train, 8),
        "validation_tp035_before_sl015_lift": round(target_valid - target_base_valid, 8),
    }


def _is_research_event(row):
    s = row.get("snapshot") or {}
    if s.get("base_momentum"):
        return True
    if _f(s.get("microstructure_score")) is not None and _f(s.get("microstructure_score")) >= float(
        os.getenv("V24_EVENT_MIN_MICRO_SCORE", "72")
    ):
        return True
    move5 = abs(_f(s.get("price_move_5s_pct")) or 0.0)
    if move5 >= float(os.getenv("V24_EVENT_MIN_ABS_MOVE_5S_PCT", "0.04")):
        return True
    seq = s.get("sequence_context") or {}
    if abs(_f(seq.get("score_delta_3s")) or 0.0) >= float(
        os.getenv("V24_EVENT_MIN_SCORE_DELTA_3S", "10")
    ):
        return True
    return False


def _non_overlapping_events(rows, horizon_seconds):
    gap_ms = max(1000, int(horizon_seconds) * 1000)
    last_by_symbol = {}
    out = []
    for row in sorted(rows, key=lambda x: int(x.get("snapshot_ms") or 0)):
        if not _is_research_event(row):
            continue
        symbol = str(row.get("symbol") or "")
        ts = int(row.get("snapshot_ms") or 0)
        last = int(last_by_symbol.get(symbol) or 0)
        if last and ts - last < gap_ms:
            continue
        out.append(row)
        last_by_symbol[symbol] = ts
    return out


def _purged_split(rows, horizon_seconds):
    if len(rows) < 2:
        return rows, []
    ordered = sorted(rows, key=lambda x: int(x.get("snapshot_ms") or 0))
    split_index = max(1, min(len(ordered) - 1, int(len(ordered) * float(os.getenv("V24_CAL_TRAIN_FRACTION", "0.70")))))
    split_ts = int(ordered[split_index].get("snapshot_ms") or 0)
    embargo_ms = int(horizon_seconds) * 1000
    train = [r for r in ordered if int(r.get("snapshot_ms") or 0) <= split_ts - embargo_ms]
    valid = [r for r in ordered if int(r.get("snapshot_ms") or 0) >= split_ts + embargo_ms]
    return train, valid


def calibrate_horizon(store, horizon_seconds):
    raw_rows = store.v24_labeled_snapshots(int(horizon_seconds), limit=20000)
    rows = _non_overlapping_events(raw_rows, int(horizon_seconds))
    n = len(rows)
    min_total = int(os.getenv("V24_CAL_MIN_EVENT_ROWS", "80"))
    if n < min_total:
        return {
            "status": "collecting",
            "horizon_seconds": int(horizon_seconds),
            "raw_n": len(raw_rows),
            "n": n,
            "minimum_required": min_total,
            "sampling": "event_nonoverlap_purged",
            "baseline": _metrics(rows),
            "groups": {},
            "auto_apply": False,
        }

    train, valid = _purged_split(rows, int(horizon_seconds))
    if len(train) < int(os.getenv("V24_CAL_MIN_TRAIN_ROWS", "40")) or len(valid) < int(
        os.getenv("V24_CAL_MIN_VALID_ROWS", "20")
    ):
        return {
            "status": "collecting_after_purge",
            "horizon_seconds": int(horizon_seconds),
            "raw_n": len(raw_rows),
            "n": n,
            "train_n": len(train),
            "validation_n": len(valid),
            "sampling": "event_nonoverlap_purged",
            "groups": {},
            "auto_apply": False,
        }
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
    for group in ("base_momentum", "microstructure", "perp", "cross_exchange", "sequence"):
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
        "raw_n": len(raw_rows),
        "n": n,
        "sampling": "event_nonoverlap_purged",
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
        "label_version": "price_tick_v3_passage",
        "auto_apply": False,
        "horizons": {},
    }
    for horizon in (5, 15, 30, 60, 120, 300, 900):
        result["horizons"][str(horizon)] = calibrate_horizon(store, horizon)
    store.set_runtime("v24_empirical_calibration", result)
    return result
