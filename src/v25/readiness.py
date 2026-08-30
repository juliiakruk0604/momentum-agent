from __future__ import annotations

import math
import os
import time


def _f(value, default=0.0):
    try:
        x = float(value)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)


def _trade_metrics(trades):
    pnls = [_f(t.get("pnl_usdt")) for t in trades]
    n = len(pnls)
    if n == 0:
        return {
            "n": 0,
            "expectancy_usdt": None,
            "median_pnl_usdt": None,
            "profit_factor": None,
            "win_rate": None,
            "lower_mean_90_usdt": None,
            "max_drawdown_pct": None,
            "perp1x_expectancy_usdt": None,
        }

    mean = sum(pnls) / n
    ordered = sorted(pnls)
    mid = n // 2
    median = ordered[mid] if n % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))
    # Keep the no-loss convention finite so readiness payloads stay valid JSON.
    profit_factor = (
        999.0 if gross_profit > 0 else None
    ) if gross_loss <= 1e-12 else gross_profit / gross_loss

    if n >= 2:
        variance = sum((x - mean) ** 2 for x in pnls) / (n - 1)
        std = math.sqrt(max(variance, 0.0))
        lower90 = mean - 1.6448536269514722 * std / math.sqrt(n)
    else:
        lower90 = None

    starting_equity = 15.0
    equity = starting_equity
    peak = starting_equity
    max_dd = 0.0
    for pnl in pnls:
        equity += pnl
        peak = max(peak, equity)
        if peak > 0:
            max_dd = min(max_dd, (equity / peak - 1.0) * 100.0)

    perp = [
        _f(t.get("perp_1x_counterfactual_pnl_usdt"))
        for t in trades
        if t.get("perp_1x_counterfactual_pnl_usdt") is not None
    ]

    return {
        "n": n,
        "expectancy_usdt": mean,
        "median_pnl_usdt": median,
        "profit_factor": profit_factor,
        "profit_factor_no_losses": gross_loss <= 1e-12 and gross_profit > 0,
        "win_rate": len(wins) / n,
        "lower_mean_90_usdt": lower90,
        "max_drawdown_pct": max_dd,
        "perp1x_expectancy_usdt": None if not perp else sum(perp) / len(perp),
    }


def _stream_health(store):
    row = store.get_runtime("v24_stream_status")
    status = None if row is None else row.get("value")
    status = status if isinstance(status, dict) else {}
    now_ms = int(time.time() * 1000)
    age_ms = now_ms - int(status.get("last_message_ms") or 0)
    max_age = int(os.getenv("V25_MAX_STREAM_AGE_MS", "5000"))

    required = {
        "bybit_spot": bool(status.get("connected")),
        "bybit_linear": bool((status.get("linear_stream") or {}).get("connected")),
        "binance": bool((status.get("binance_stream") or {}).get("connected")),
        "okx": bool((status.get("okx_stream") or {}).get("connected")),
    }
    return {
        "healthy": all(required.values()) and age_ms <= max_age,
        "age_ms": age_ms,
        "max_age_ms": max_age,
        "feeds": required,
    }


def evaluate_v25_readiness(store):
    shadow_row = store.get_runtime("v25_hybrid_shadow_summary")
    shadow = None if shadow_row is None else shadow_row.get("value")
    shadow = shadow if isinstance(shadow, dict) else {}

    evidence_row = store.get_runtime("v25_evidence")
    evidence = None if evidence_row is None else evidence_row.get("value")
    evidence = evidence if isinstance(evidence, dict) else {}
    promotion = evidence.get("promotion") or {}

    trades = shadow.get("recent_trades") or []
    metrics = _trade_metrics(trades)
    stream = _stream_health(store)

    blockers = []
    warnings = []

    min_trades = int(os.getenv("V25_READINESS_MIN_FORWARD_TRADES", "30"))
    min_pf = float(os.getenv("V25_READINESS_MIN_PROFIT_FACTOR", "1.20"))
    max_dd = float(os.getenv("V25_READINESS_MAX_DRAWDOWN_PCT", "10.0"))

    if not bool(promotion.get("candidate_promotable")):
        blockers.append("purged_validation_not_promotable")
    if int(metrics.get("n") or 0) < min_trades:
        blockers.append("insufficient_forward_trades")
    if metrics.get("expectancy_usdt") is None or float(metrics["expectancy_usdt"]) <= 0.0:
        blockers.append("forward_expectancy_nonpositive")
    if metrics.get("lower_mean_90_usdt") is None or float(metrics["lower_mean_90_usdt"]) <= 0.0:
        blockers.append("forward_mean_lower90_nonpositive")
    if metrics.get("profit_factor") is None or float(metrics["profit_factor"]) < min_pf:
        blockers.append("forward_profit_factor_low")
    if abs(float(metrics.get("max_drawdown_pct") or 0.0)) > max_dd:
        blockers.append("forward_drawdown_too_high")
    if not stream.get("healthy"):
        blockers.append("market_data_unhealthy")

    spot_exp = metrics.get("expectancy_usdt")
    perp_exp = metrics.get("perp1x_expectancy_usdt")
    if spot_exp is not None and perp_exp is not None and spot_exp <= 0 < perp_exp:
        warnings.append("possible_fee_limited_edge_perp1x_counterfactual_positive")

    diagnostics = shadow.get("diagnostics") or {}
    if int(diagnostics.get("base_momentum_attached") or 0) <= 0:
        warnings.append("no_current_base_signal_attached")

    candidate_ready = len(blockers) == 0
    return {
        "engine": "MomentumAgentV2.5",
        "candidate_ready": candidate_ready,
        "live_ready": False,
        "live_execution": False,
        "blockers": blockers,
        "warnings": warnings,
        "thresholds": {
            "min_forward_trades": min_trades,
            "min_profit_factor": min_pf,
            "max_drawdown_pct": max_dd,
            "requires_positive_lower_mean_90": True,
            "requires_purged_validation_promotion": True,
        },
        "forward": metrics,
        "purged_validation_promotion": promotion,
        "stream": stream,
        "diagnostics": diagnostics,
        "note": "candidate_ready never enables real orders; live execution remains manually disabled",
    }
