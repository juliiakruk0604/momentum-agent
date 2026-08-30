from __future__ import annotations

import os

import pandas as pd


def _runtime_age_seconds(row, time_fields=("finished_at","last_updated_at","started_at")):
    value = None if row is None else row.get("value")
    if not isinstance(value, dict):
        return None
    raw = None
    for field in time_fields:
        if value.get(field):
            raw = value.get(field)
            break
    if raw is None:
        return None
    try:
        ts = pd.Timestamp(raw)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        return max(0.0, (pd.Timestamp.now(tz="UTC") - ts).total_seconds())
    except Exception:
        return None


def evaluate_v2_readiness(store):
    reasons = []
    warnings = []

    bt_row = store.get_runtime("v2_backtest_summary")
    bt = None if bt_row is None else bt_row.get("value")
    shadow_row = store.get_runtime("v2_shadow_portfolio")
    shadow = None if shadow_row is None else shadow_row.get("value")
    scan_row = store.get_runtime("v2_scan")
    scan = None if scan_row is None else scan_row.get("value")

    if not isinstance(bt, dict):
        reasons.append("v2_backtest_not_started")
        bt = {}
    if not bt.get("complete", False):
        reasons.append("v2_backtest_not_complete")

    metrics = bt.get("metrics") or {}
    n = int(metrics.get("n") or 0)
    expectancy = float(metrics.get("expectancy_usdt") or 0.0)
    pf = metrics.get("profit_factor")
    dd = float(metrics.get("max_drawdown_pct") or 0.0)

    min_backtest_trades = int(os.getenv("V2_GATE_MIN_BACKTEST_TRADES", "30"))
    min_pf = float(os.getenv("V2_GATE_MIN_PROFIT_FACTOR", "1.20"))
    max_dd_abs = float(os.getenv("V2_GATE_MAX_DRAWDOWN_PCT", "10"))
    if n < min_backtest_trades:
        reasons.append("insufficient_backtest_trades")
    if expectancy <= 0:
        reasons.append("nonpositive_backtest_expectancy")
    if pf is None or float(pf) < min_pf:
        reasons.append("profit_factor_below_gate")
    if dd < -max_dd_abs:
        reasons.append("backtest_drawdown_too_large")

    sensitivity = bt.get("score_sensitivity") or {}
    stable_positive = 0
    evaluated = 0
    for threshold in ("65", "70", "75"):
        row = sensitivity.get(threshold) or {}
        count = int(row.get("n") or 0)
        if count < 5:
            continue
        evaluated += 1
        exp = float(row.get("expectancy_usdt") or 0.0)
        row_pf = row.get("profit_factor")
        if exp > 0 and row_pf is not None and float(row_pf) > 1.0:
            stable_positive += 1
    if evaluated < 2:
        reasons.append("insufficient_parameter_sensitivity")
    elif stable_positive < 2:
        reasons.append("edge_not_stable_across_score_thresholds")

    folds = bt.get("walk_forward_7d") or []
    active_folds = [f for f in folds if int(((f.get("metrics") or {}).get("n") or 0)) > 0]
    if len(active_folds) < 3:
        reasons.append("insufficient_walk_forward_folds")
    else:
        positive_folds = sum(
            1 for f in active_folds
            if float(((f.get("metrics") or {}).get("net_pnl_usdt") or 0.0)) > 0
        )
        if positive_folds / len(active_folds) < 0.5:
            reasons.append("walk_forward_instability")

    mc = bt.get("monte_carlo") or {}
    if mc:
        p_loss = float(mc.get("probability_finish_below_start") or 0.0)
        if p_loss > float(os.getenv("V2_GATE_MAX_MONTE_CARLO_LOSS_PROB", "0.45")):
            reasons.append("monte_carlo_loss_probability_too_high")
    elif n >= 5:
        warnings.append("monte_carlo_unavailable")

    shadow_trades = 0
    shadow_expectancy = 0.0
    current_version = os.getenv("V2_STRATEGY_VERSION", "2.1")
    if isinstance(shadow, dict):
        trades = [
            t for t in (shadow.get("trades") or [])
            if str(t.get("strategy_version") or "legacy") == current_version
        ]
        shadow_trades = len(trades)
        if trades:
            shadow_expectancy = sum(float(t.get("pnl_usdt") or 0.0) for t in trades) / len(trades)
    min_shadow_trades = int(os.getenv("V2_GATE_MIN_SHADOW_TRADES", "20"))
    if shadow_trades < min_shadow_trades:
        reasons.append("insufficient_forward_shadow_trades")
    if shadow_trades >= 5 and shadow_expectancy <= 0:
        reasons.append("nonpositive_forward_shadow_expectancy")

    # Current V2 historical dataset intentionally lacks historical orderbook and perp aux parity.
    if bt and not bt.get("microstructure_history_included", False):
        warnings.append("historical_microstructure_not_available")
    if bt and not bt.get("perp_aux_features_included", False):
        warnings.append("historical_perp_aux_not_included")

    # Live key scope must be safer than the current connected account state.
    account_row = store.get_runtime("promo_account_snapshot")
    account = None if account_row is None else account_row.get("value")
    forbidden = (account or {}).get("forbidden_permissions") or {}
    if forbidden.get("contract_trade"):
        reasons.append("api_contract_permission_present")
    if forbidden.get("options"):
        reasons.append("api_options_permission_present")
    if forbidden.get("withdraw"):
        reasons.append("api_withdraw_permission_present")

    worker_error = store.get_runtime("worker_error")
    worker_error_value = None if worker_error is None else worker_error.get("value")
    if worker_error_value:
        reasons.append("worker_error_present")

    market_error_row = store.get_runtime("v2_market_error")
    market_error = None if market_error_row is None else market_error_row.get("value")
    if market_error:
        reasons.append("v2_market_error_present")

    market_hb = store.get_runtime("v2_market_heartbeat")
    market_age = _runtime_age_seconds(market_hb)
    market_stale_after = float(os.getenv("V2_GATE_MARKET_STALE_SECONDS", "90"))
    if market_age is None:
        reasons.append("v2_market_heartbeat_missing")
    elif market_age > market_stale_after:
        reasons.append("v2_market_heartbeat_stale")

    research_error_row = store.get_runtime("v2_backtest_error")
    research_error = None if research_error_row is None else research_error_row.get("value")
    if research_error:
        reasons.append("v2_backtest_error_present")

    research_hb = store.get_runtime("research_worker_heartbeat")
    research_age = _runtime_age_seconds(research_hb)
    research_stale_after = float(os.getenv("V2_GATE_RESEARCH_STALE_SECONDS", "600"))
    if research_age is None:
        warnings.append("research_heartbeat_missing")
    elif research_age > research_stale_after:
        warnings.append("research_heartbeat_stale")

    return {
        "engine": "MomentumAgentV2",
        "live_ready": len(reasons) == 0,
        "live_execution": False,
        "reasons": reasons,
        "warnings": warnings,
        "backtest": {
            "complete": bool(bt.get("complete")),
            "n": n,
            "expectancy_usdt": expectancy,
            "profit_factor": pf,
            "max_drawdown_pct": dd,
            "stable_positive_thresholds": stable_positive,
            "sensitivity_thresholds_evaluated": evaluated,
        },
        "forward_shadow": {
            "strategy_version": current_version,
            "closed_trades": shadow_trades,
            "expectancy_usdt": shadow_expectancy,
        },
        "services": {
            "v2_market_age_seconds": market_age,
            "research_age_seconds": research_age,
            "v2_market_error": market_error,
            "v2_backtest_error": research_error,
        },
        "current_scan": {
            "generated_at": None if not isinstance(scan, dict) else scan.get("generated_at"),
            "candidate_count": None if not isinstance(scan, dict) else scan.get("candidate_count"),
        },
    }
