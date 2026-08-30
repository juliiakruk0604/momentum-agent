from __future__ import annotations

import os
import pandas as pd


def _ts(value):
    x = pd.Timestamp(value)
    return x.tz_localize("UTC") if x.tzinfo is None else x.tz_convert("UTC")


def confirmation_decision(armed, current, now=None):
    now = pd.Timestamp.now(tz="UTC") if now is None else _ts(now)
    reasons = []

    if not isinstance(armed, dict):
        return {"confirmed": False, "reasons": ["not_armed"]}
    if not isinstance(current, dict):
        return {"confirmed": False, "reasons": ["candidate_missing"]}
    if current.get("symbol") != armed.get("symbol"):
        reasons.append("symbol_changed")

    armed_at = _ts(armed.get("armed_at"))
    age = (now - armed_at).total_seconds()
    if age < float(os.getenv("V23_MIN_CONFIRM_SECONDS", "35")):
        reasons.append("confirmation_too_early")
    if age > float(os.getenv("V23_MAX_CONFIRM_SECONDS", "150")):
        reasons.append("confirmation_expired")

    if current.get("action") != "SHADOW_READY":
        reasons.append("current_not_ready")

    score = float(current.get("score") or 0.0)
    armed_score = float(armed.get("score") or 0.0)
    if score < float(os.getenv("V23_MIN_SCORE", "76")):
        reasons.append("score_low")
    if armed_score > 0 and score < armed_score * float(os.getenv("V23_MIN_SCORE_RETENTION", "0.82")):
        reasons.append("score_collapsed")

    flow = float(current.get("flow_score") or 0.0)
    if flow < float(os.getenv("V23_MIN_FLOW_SCORE", "55")):
        reasons.append("flow_low")

    tf = current.get("trade_flow") or {}
    if float(tf.get("flow_confidence") or 0.0) < float(os.getenv("V23_MIN_FLOW_CONFIDENCE", "0.60")):
        reasons.append("flow_confidence_low")
    if float(tf.get("recent_buy_ratio", tf.get("buy_ratio", 0.5))) < float(os.getenv("V23_MIN_BUY_RATIO", "0.58")):
        reasons.append("buy_ratio_low")

    book = current.get("book") or {}
    if float(book.get("spread_pct") or 999.0) > float(os.getenv("V23_MAX_SPREAD_PCT", "0.08")):
        reasons.append("spread_wide")
    if float(book.get("depth_usdt") or 0.0) < float(os.getenv("V23_MIN_DEPTH_USDT", "5000")):
        reasons.append("depth_low")

    armed_price = float(armed.get("signal_price") or 0.0)
    current_price = float(current.get("signal_price") or 0.0)
    if armed_price <= 0 or current_price <= 0:
        reasons.append("price_missing")
    else:
        continuation = (current_price / armed_price - 1.0) * 100.0
        if continuation < float(os.getenv("V23_MIN_CONTINUATION_PCT", "-0.10")):
            reasons.append("price_failed_to_hold")
        if continuation > float(os.getenv("V23_MAX_EXTENSION_PCT", "1.25")):
            reasons.append("too_extended")

    accel = current.get("flow_acceleration") or {}
    if float(accel.get("flow_score_delta") or 0.0) < float(os.getenv("V23_MIN_FLOW_DELTA", "-8")):
        reasons.append("flow_deteriorating")

    return {
        "confirmed": len(reasons) == 0,
        "reasons": reasons,
        "age_seconds": round(age, 2),
        "armed_score": armed_score,
        "current_score": score,
        "current_flow_score": flow,
    }
