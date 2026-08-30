from __future__ import annotations

import os
import time


def _value(store, key):
    row = store.get_runtime(key)
    return None if row is None else row.get("value")


def evaluate_v24_readiness(store):
    reasons = []
    warnings = []
    now_ms = int(time.time() * 1000)

    status = _value(store, "v24_stream_status")
    parity = _value(store, "v24_stream_parity")
    shadow = _value(store, "v24_event_shadow")
    stats = _value(store, "v24_feature_snapshot_stats")
    labels = _value(store, "v24_feature_label_stats")

    if not isinstance(status, dict) or not status.get("connected"):
        reasons.append("bybit_spot_stream_not_connected")
    else:
        age = now_ms - int(status.get("last_message_ms") or 0)
        if age > int(float(os.getenv("V24_GATE_MAX_STREAM_AGE_SECONDS", "5")) * 1000):
            reasons.append("bybit_spot_stream_stale")
        if int(status.get("reconnects") or 0) > int(os.getenv("V24_GATE_MAX_RECONNECTS", "5")):
            warnings.append("bybit_spot_reconnects_high")

        linear = status.get("linear_stream") or {}
        if not linear.get("connected"):
            reasons.append("bybit_linear_stream_not_connected")

        binance = status.get("binance_stream") or {}
        if not binance.get("connected"):
            warnings.append("binance_stream_unavailable")

        okx = status.get("okx_stream") or {}
        if not okx.get("connected"):
            warnings.append("okx_stream_unavailable")

    if not isinstance(parity, dict) or not parity.get("healthy"):
        reasons.append("websocket_rest_parity_not_healthy")

    snapshot_count = int((stats or {}).get("snapshots") or 0)
    if snapshot_count < int(os.getenv("V24_GATE_MIN_FEATURE_SNAPSHOTS", "2000")):
        reasons.append("insufficient_v24_feature_snapshots")

    horizon_counts = {}
    for row in labels or []:
        horizon_counts[str(row.get("horizon_seconds"))] = int(row.get("n") or 0)
    for horizon in (5, 15, 30, 60):
        if horizon_counts.get(str(horizon), 0) < int(
            os.getenv("V24_GATE_MIN_LABELS_PER_HORIZON", "500")
        ):
            reasons.append(f"insufficient_v24_labels_{horizon}s")

    trades = [] if not isinstance(shadow, dict) else (shadow.get("trades") or [])
    if len(trades) < int(os.getenv("V24_GATE_MIN_SHADOW_TRADES", "30")):
        reasons.append("insufficient_v24_shadow_trades")
    if len(trades) >= 5:
        expectancy = sum(float(t.get("pnl_usdt") or 0.0) for t in trades) / len(trades)
        if expectancy <= 0:
            reasons.append("v24_shadow_expectancy_nonpositive")
    else:
        expectancy = None

    # External feeds and perp context are evidence-only until a forward
    # calibration proves that they add out-of-sample uplift.
    warnings.append("perp_and_cross_exchange_context_not_yet_authorized_in_signal")

    return {
        "engine": "MomentumAgentV2.4",
        "live_ready": len(reasons) == 0,
        "live_execution": False,
        "reasons": list(dict.fromkeys(reasons)),
        "warnings": list(dict.fromkeys(warnings)),
        "streams": status,
        "parity": parity,
        "feature_archive": stats,
        "label_counts": horizon_counts,
        "forward_shadow": {
            "closed_trades": len(trades),
            "expectancy_usdt": expectancy,
        },
    }
