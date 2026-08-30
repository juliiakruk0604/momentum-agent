from __future__ import annotations

import os
import pandas as pd

from src.v2.provider import BybitV2Provider
from src.v2.research import trade_metrics, grouped_trade_metrics
from .runner import evaluate_runner_path


KEY = "v22_shadow_portfolio"


def _now():
    return pd.Timestamp.now(tz="UTC")


def _ms(ts):
    return int(pd.Timestamp(ts).timestamp() * 1000)


def _new_state():
    start = float(os.getenv("V22_SHADOW_START_EQUITY_USDT", "15"))
    return {
        "strategy_version": "2.2",
        "started_at": str(_now()),
        "starting_equity_usdt": start,
        "cash_usdt": start,
        "equity_usdt": start,
        "realized_pnl_usdt": 0.0,
        "open_position": None,
        "trades": [],
        "seen_signals": [],
        "last_action": None,
        "last_rejection": None,
        "last_updated_at": str(_now()),
    }


def _load(store):
    row = store.get_runtime(KEY)
    if row is None or not isinstance(row.get("value"), dict):
        state = _new_state()
        store.set_runtime(KEY, state)
        return state
    return row["value"]


def _save(store, state):
    state["last_updated_at"] = str(_now())
    store.set_runtime(KEY, state)


def _signal_key(candidate):
    return f"{candidate.get('signal_time')}:{candidate.get('symbol')}:{candidate.get('setup')}"


def _today_stats(state):
    today = _now().date()
    pnl = 0.0
    count = 0
    for trade in state.get("trades") or []:
        try:
            ts = pd.Timestamp(trade["exit_time"])
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
        except Exception:
            continue
        if ts.date() == today:
            pnl += float(trade.get("pnl_usdt") or 0.0)
            count += 1
    return pnl, count


def _update_open(provider, state):
    pos = state.get("open_position")
    if not pos:
        return

    start = pd.Timestamp(pos["entry_time"])
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    now = _now()
    bars = provider.kline_range(
        pos["symbol"], "1",
        _ms(start - pd.Timedelta(minutes=1)),
        _ms(now),
        category="spot",
    )
    ticker = provider.ticker(pos["symbol"], "spot")
    bid = float(ticker.get("bid1Price") or ticker.get("lastPrice") or 0.0)

    runner = evaluate_runner_path(
        bars1m=bars,
        entry_time=start,
        entry_price=float(pos["entry_price"]),
        notional_usdt=float(pos["entry_notional_usdt"]),
        initial_stop_pct=float(pos["initial_stop_pct"]),
        mark_price=bid,
        fee_rate=float(os.getenv("V2_FEE_RATE", "0.001")),
        entry_slippage_pct=0.0,
        exit_slippage_pct=float(os.getenv("V2_EXIT_SLIPPAGE_PCT", "0.05")),
        partial_r=float(pos["runner"]["partial_r"]),
        partial_fraction=float(pos["runner"]["partial_fraction"]),
        trail_pct=float(pos["runner"]["trail_pct"]),
        breakeven_buffer_pct=float(os.getenv("V22_BREAKEVEN_BUFFER_PCT", "0.30")),
        max_hold_minutes=int(pos["runner"]["max_hold_minutes"]),
    )
    pos["runner_state"] = runner.to_dict()

    if runner.status == "CLOSED":
        state["cash_usdt"] = float(pos["cash_after_entry"]) + float(runner.equity_value_usdt)
        state["realized_pnl_usdt"] = float(state.get("realized_pnl_usdt") or 0.0) + float(runner.total_pnl_usdt)
        trade = {
            **pos,
            "exit_time": runner.final_exit_time,
            "exit_price": runner.final_exit_price,
            "exit_reason": runner.final_exit_reason,
            "pnl_usdt": runner.total_pnl_usdt,
            "partial_hit": runner.partial_hit,
            "partial_exit_time": runner.partial_exit_time,
            "partial_exit_price": runner.partial_exit_price,
            "highest_price": runner.highest_price,
        }
        state["trades"] = (state.get("trades") or []) + [trade]
        state["trades"] = state["trades"][-250:]
        state["open_position"] = None
        state["equity_usdt"] = float(state["cash_usdt"])
        state["last_action"] = {
            "time": str(_now()),
            "action": "V22_SHADOW_CLOSE",
            "symbol": trade["symbol"],
            "reason": trade["exit_reason"],
            "pnl_usdt": trade["pnl_usdt"],
        }
    else:
        state["equity_usdt"] = float(pos["cash_after_entry"]) + float(runner.equity_value_usdt)


def _maybe_open(provider, store, state):
    if state.get("open_position") is not None:
        return

    row = store.get_runtime("v22_fast_scan")
    scan = None if row is None else row.get("value")
    if not isinstance(scan, dict):
        return

    today_pnl, today_count = _today_stats(state)
    if today_pnl <= -float(os.getenv("V22_DAILY_STOP_USDT", "0.50")):
        state["last_rejection"] = {"time": str(_now()), "reason": "daily_loss_stop"}
        return
    if today_count >= int(os.getenv("V22_MAX_TRADES_PER_DAY", "3")):
        state["last_rejection"] = {"time": str(_now()), "reason": "daily_trade_limit"}
        return

    candidates = [x for x in scan.get("candidates") or [] if x.get("action") == "SHADOW_READY"]
    if not candidates:
        return

    c = candidates[0]
    key = _signal_key(c)
    seen = set(state.get("seen_signals") or [])
    if key in seen:
        return
    seen.add(key)
    state["seen_signals"] = list(seen)[-1000:]

    micro = c.get("book") or {}
    best_ask = float(micro.get("best_ask") or 0.0)
    notional = float((c.get("risk") or {}).get("notional_usdt") or 0.0)
    if best_ask <= 0 or notional <= 0:
        state["last_rejection"] = {"time": str(_now()), "reason": "invalid_entry"}
        return

    fee_rate = float(os.getenv("V2_FEE_RATE", "0.001"))
    entry_slip = float(os.getenv("V2_ENTRY_SLIPPAGE_PCT", "0.05"))
    entry = best_ask * (1.0 + entry_slip / 100.0)
    fee = notional * fee_rate
    total_cost = notional + fee
    if total_cost > float(state["cash_usdt"]):
        state["last_rejection"] = {"time": str(_now()), "reason": "insufficient_shadow_cash"}
        return

    cash_after = float(state["cash_usdt"]) - total_cost
    state["cash_usdt"] = cash_after
    state["equity_usdt"] = cash_after + notional
    state["open_position"] = {
        "strategy_version": "2.2",
        "signal_key": key,
        "symbol": c["symbol"],
        "setup": c["setup"],
        "regime": c["regime"],
        "score": float(c["score"]),
        "entry_time": str(_now()),
        "entry_price": entry,
        "entry_notional_usdt": notional,
        "entry_fee_usdt": fee,
        "cash_after_entry": cash_after,
        "initial_stop_pct": float(c["initial_stop_pct"]),
        "runner": c["runner"],
        "fast_features": c["fast_features"],
        "flow_score": c["flow_score"],
        "book": c["book"],
        "trade_flow": c["trade_flow"],
        "runner_state": None,
    }
    state["last_action"] = {
        "time": str(_now()),
        "action": "V22_SHADOW_OPEN",
        "symbol": c["symbol"],
        "score": c["score"],
        "entry_price": entry,
        "notional_usdt": notional,
    }


def summary(state):
    start = float(state.get("starting_equity_usdt") or 15.0)
    trades = state.get("trades") or []
    metrics = trade_metrics(trades, starting_equity=start)
    return {
        "mode": "V2.2_SENSITIVE_RUNNER_SHADOW",
        "strategy_version": "2.2",
        "starting_equity_usdt": start,
        "current_equity_usdt": round(float(state.get("equity_usdt") or start), 8),
        "net_pnl_usdt": round(float(state.get("equity_usdt") or start) - start, 8),
        "realized_pnl_usdt": round(float(state.get("realized_pnl_usdt") or 0.0), 8),
        "open_position": state.get("open_position"),
        "closed_trades": len(trades),
        "metrics": metrics,
        "by_regime": grouped_trade_metrics(trades, "regime", starting_equity=start),
        "last_action": state.get("last_action"),
        "last_rejection": state.get("last_rejection"),
        "recent_trades": trades[-20:],
        "last_updated_at": state.get("last_updated_at"),
        "live_execution": False,
    }


def process_v22_shadow(store, provider=None):
    provider = provider or BybitV2Provider()
    state = _load(store)
    _update_open(provider, state)
    _maybe_open(provider, store, state)
    _save(store, state)
    return summary(state)
