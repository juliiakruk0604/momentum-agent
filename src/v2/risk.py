from __future__ import annotations

import os

from .models import RiskDecision


def execution_cost_pct(micro):
    fee_rate = float(os.getenv("V2_FEE_RATE", "0.001"))
    entry_slip = float(os.getenv("V2_ENTRY_SLIPPAGE_PCT", "0.05"))
    exit_slip = float(os.getenv("V2_EXIT_SLIPPAGE_PCT", "0.05"))
    spread = float((micro or {}).get("spread_pct") or 0.0)
    return 2.0 * fee_rate * 100.0 + entry_slip + exit_slip + spread


def cost_adjusted_levels(stop_pct, target_pct, micro=None):
    cost = execution_cost_pct(micro or {})
    min_net_rr = float(os.getenv("V2_MIN_NET_RR", "1.60"))
    max_target = float(os.getenv("V2_MAX_TARGET_PCT", "6.0"))
    stop = float(stop_pct)
    target = float(target_pct)
    effective_risk = stop + cost
    required_target = cost + min_net_rr * effective_risk
    adjusted_target = max(target, required_target)
    blocked = adjusted_target > max_target
    adjusted_target = min(adjusted_target, max_target)
    net_reward = max(0.0, adjusted_target - cost)
    net_risk = effective_risk
    net_rr = 0.0 if net_risk <= 0 else net_reward / net_risk
    return {
        "cost_pct": cost,
        "stop_pct": stop,
        "target_pct": adjusted_target,
        "net_reward_pct": net_reward,
        "net_risk_pct": net_risk,
        "net_rr": net_rr,
        "required_target_pct": required_target,
        "blocked": blocked or net_rr < min_net_rr,
    }


def microstructure(provider, symbol):
    book = provider.orderbook(symbol, limit=25)
    bids, asks = book["bids"], book["asks"]
    if not bids or not asks:
        return {"ok": False, "spread_pct": None, "depth_usdt": 0.0, "reason": "empty_orderbook"}
    bid, ask = bids[0][0], asks[0][0]
    spread_pct = (ask / bid - 1.0) * 100.0 if bid > 0 else 999.0
    mid = (bid + ask) / 2.0
    band = float(os.getenv("V2_DEPTH_BAND_PCT", "0.35")) / 100.0
    depth_bid = sum(p*q for p,q in bids if p >= mid * (1.0 - band))
    depth_ask = sum(p*q for p,q in asks if p <= mid * (1.0 + band))
    return {
        "ok": True,
        "best_bid": bid,
        "best_ask": ask,
        "spread_pct": spread_pct,
        "depth_usdt": min(depth_bid, depth_ask),
    }


def risk_decision(candidate, micro, equity_usdt=15.0, realized_today_usdt=0.0, has_open_position=False):
    blockers = []
    max_notional = float(os.getenv("V2_MAX_NOTIONAL_USDT", "5"))
    max_spread = float(os.getenv("V2_MAX_SPREAD_PCT", "0.12"))
    min_depth = float(os.getenv("V2_MIN_DEPTH_USDT", "5000"))
    daily_stop = float(os.getenv("V2_DAILY_STOP_USDT", "0.50"))

    if has_open_position:
        blockers.append("single_position_limit")
    if realized_today_usdt <= -daily_stop:
        blockers.append("daily_loss_stop")
    if not micro.get("ok"):
        blockers.append("orderbook_unavailable")
    if micro.get("spread_pct") is None or float(micro["spread_pct"]) > max_spread:
        blockers.append("spread_too_wide")
    if float(micro.get("depth_usdt") or 0.0) < min_depth:
        blockers.append("insufficient_depth")

    levels = cost_adjusted_levels(candidate.stop_pct, candidate.target_pct, micro)
    if levels["blocked"]:
        blockers.append("net_rr_below_gate")

    notional = min(max_notional, max(0.0, float(equity_usdt) * 0.34))
    risk_usdt = notional * float(levels["net_risk_pct"]) / 100.0
    max_risk = float(os.getenv("V2_MAX_RISK_USDT", "0.15"))
    if risk_usdt > max_risk:
        scale = max_risk / max(risk_usdt, 1e-9)
        notional *= scale
        risk_usdt = notional * float(levels["net_risk_pct"]) / 100.0

    if notional < 5.0:
        blockers.append("below_typical_spot_minimum")

    return RiskDecision(
        allowed=len(blockers) == 0,
        notional_usdt=round(notional, 6),
        risk_usdt=round(risk_usdt, 6),
        stop_pct=float(levels["stop_pct"]),
        target_pct=float(levels["target_pct"]),
        blockers=blockers,
    )
