from __future__ import annotations

import os
import pandas as pd


def _row_value(store, key):
    row = store.get_runtime(key)
    return None if row is None else row.get("value")


def _age_seconds(value, fields):
    if not isinstance(value, dict):
        return None
    raw = None
    for field in fields:
        if value.get(field):
            raw = value.get(field)
            break
    if not raw:
        return None
    try:
        ts = pd.Timestamp(raw)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return max(0.0, (pd.Timestamp.now(tz="UTC") - ts).total_seconds())
    except Exception:
        return None


def evaluate_v22_readiness(store):
    reasons = []
    warnings = []

    fast = _row_value(store, "v22_fast_scan")
    shadow = _row_value(store, "v22_shadow_portfolio")
    replay = _row_value(store, "v22_runner_replay_summary")
    market_hb = _row_value(store, "v2_market_heartbeat")
    flow_stats = _row_value(store, "v22_flow_snapshot_stats")

    market_age = _age_seconds(market_hb, ("finished_at","started_at"))
    if market_age is None:
        reasons.append("market_heartbeat_missing")
    elif market_age > float(os.getenv("V22_GATE_MARKET_STALE_SECONDS", "90")):
        reasons.append("market_heartbeat_stale")

    market_error = _row_value(store, "v2_market_error")
    if market_error:
        reasons.append("market_service_error_present")

    if not isinstance(fast, dict):
        reasons.append("fast_scan_not_started")
    elif fast.get("errors"):
        warnings.append("fast_scan_has_symbol_errors")

    trades = [] if not isinstance(shadow, dict) else (shadow.get("trades") or [])
    min_forward = int(os.getenv("V22_GATE_MIN_FORWARD_TRADES", "30"))
    if len(trades) < min_forward:
        reasons.append("insufficient_v22_forward_trades")
    if len(trades) >= 5:
        avg = sum(float(t.get("pnl_usdt") or 0.0) for t in trades) / len(trades)
        if avg <= 0:
            reasons.append("nonpositive_v22_forward_expectancy")

    if not isinstance(replay, dict):
        reasons.append("runner_replay_not_started")
    else:
        if not replay.get("complete"):
            reasons.append("runner_replay_not_complete")
        matched = int(replay.get("matched_trades") or 0)
        if matched < int(os.getenv("V22_GATE_MIN_REPLAY_TRADES", "30")):
            reasons.append("insufficient_runner_replay_trades")
        runner_m = replay.get("runner_metrics") or {}
        fixed_m = replay.get("fixed_metrics") or {}
        if matched >= 5:
            if float(runner_m.get("expectancy_usdt") or 0.0) <= 0:
                reasons.append("runner_expectancy_nonpositive")
            if float(replay.get("runner_minus_fixed_total_usdt") or 0.0) <= 0:
                reasons.append("runner_not_better_than_fixed")
            runner_dd = float(runner_m.get("max_drawdown_pct") or 0.0)
            fixed_dd = float(fixed_m.get("max_drawdown_pct") or 0.0)
            if runner_dd < min(-15.0, fixed_dd * 1.5):
                warnings.append("runner_drawdown_materially_worse")

    snapshots = 0 if not isinstance(flow_stats, dict) else int(flow_stats.get("snapshots") or 0)
    if snapshots < int(os.getenv("V22_GATE_MIN_FLOW_SNAPSHOTS", "500")):
        reasons.append("insufficient_orderflow_archive")

    # We currently cannot reconstruct historical order books/trade flow perfectly.
    # Therefore the fast trigger must earn its evidence through forward shadow data.
    warnings.append("historical_orderflow_parity_unavailable_forward_shadow_required")

    account = _row_value(store, "promo_account_snapshot")
    if not isinstance(account, dict):
        reasons.append("api_permission_snapshot_missing")
        account = {}
    forbidden = account.get("forbidden_permissions") or {}
    if forbidden.get("withdraw"):
        reasons.append("api_withdraw_permission_present")
    if forbidden.get("contract_trade"):
        reasons.append("api_contract_permission_present")
    if forbidden.get("options"):
        reasons.append("api_options_permission_present")

    return {
        "engine": "MomentumAgentV2.2",
        "live_ready": len(reasons) == 0,
        "live_execution": False,
        "reasons": reasons,
        "warnings": warnings,
        "forward_shadow": {
            "closed_trades": len(trades),
        },
        "runner_replay": replay,
        "orderflow_archive": flow_stats,
        "market_age_seconds": market_age,
        "last_fast_scan": None if not isinstance(fast, dict) else fast.get("generated_at"),
    }
