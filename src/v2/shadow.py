from __future__ import annotations

import os
import pandas as pd

from .provider import BybitV2Provider
from .research import trade_metrics, monte_carlo
from .simulator import find_barrier_exit


KEY = "v2_shadow_portfolio"


def _now():
    return pd.Timestamp.now(tz="UTC")


def _ms(ts):
    return int(pd.Timestamp(ts).timestamp() * 1000)


def _new_state():
    start = float(os.getenv("V2_SHADOW_START_EQUITY_USDT", "15"))
    return {
        "version": 2,
        "started_at": str(_now()),
        "starting_equity_usdt": start,
        "cash_usdt": start,
        "equity_usdt": start,
        "open_position": None,
        "trades": [],
        "seen_signal_keys": [],
        "last_entry_by_setup": {},
        "fees_usdt": 0.0,
        "realized_pnl_usdt": 0.0,
        "unrealized_pnl_usdt": 0.0,
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


def _today_realized(state):
    today = _now().date().isoformat()
    total = 0.0
    count = 0
    for trade in state.get("trades") or []:
        raw = trade.get("exit_time")
        if not raw:
            continue
        try:
            ts = pd.Timestamp(raw)
            if ts.tzinfo is None:
                ts = ts.tz_localize("UTC")
        except Exception:
            continue
        if ts.date().isoformat() == today:
            total += float(trade.get("pnl_usdt") or 0.0)
            count += 1
    return total, count


def _signal_key(scan, candidate):
    generated = str(scan.get("generated_at") or "")
    bucket = generated[:16]
    return f"{bucket}:{candidate.get('symbol')}:{candidate.get('setup')}"


def _close(state, event):
    pos = state.get("open_position")
    if not pos:
        return

    fee_rate = float(os.getenv("V2_FEE_RATE", "0.001"))
    exit_price = float(event["exit_price"])
    qty = float(pos["quantity"])
    gross_exit = qty * exit_price
    exit_fee = gross_exit * fee_rate
    pnl = gross_exit - exit_fee - float(pos["entry_notional_usdt"]) - float(pos["entry_fee_usdt"])

    state["cash_usdt"] = float(state["cash_usdt"]) + gross_exit - exit_fee
    state["fees_usdt"] = float(state.get("fees_usdt") or 0.0) + exit_fee
    state["realized_pnl_usdt"] = float(state.get("realized_pnl_usdt") or 0.0) + pnl
    state["unrealized_pnl_usdt"] = 0.0

    trade = {
        **pos,
        "exit_time": str(event["exit_time"]),
        "exit_price": exit_price,
        "exit_reason": str(event["exit_reason"]),
        "exit_fee_usdt": round(exit_fee, 8),
        "pnl_usdt": round(pnl, 8),
        "return_pct": round(pnl / max(float(pos["entry_notional_usdt"]), 1e-9) * 100.0, 6),
    }
    state["trades"] = (state.get("trades") or []) + [trade]
    state["trades"] = state["trades"][-250:]
    state["open_position"] = None
    state["last_action"] = {
        "time": str(_now()),
        "action": "SHADOW_CLOSE",
        "symbol": trade["symbol"],
        "reason": trade["exit_reason"],
        "pnl_usdt": trade["pnl_usdt"],
    }


def _update_open_position(provider, state):
    pos = state.get("open_position")
    if not pos:
        return

    now = _now()
    start = pd.Timestamp(pos["entry_time"])
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    bars = provider.kline_range(
        pos["symbol"],
        "1",
        _ms(start - pd.Timedelta(minutes=1)),
        _ms(now),
        category="spot",
    )
    event = find_barrier_exit(
        bars,
        start,
        float(pos["stop_price"]),
        float(pos["target_price"]),
        exit_slippage_pct=float(os.getenv("V2_EXIT_SLIPPAGE_PCT", "0.05")),
        max_hold_minutes=int(os.getenv("V2_SHADOW_MAX_HOLD_MINUTES", "720")),
    )
    if event is not None:
        _close(state, event)
        return

    ticker = provider.ticker(pos["symbol"], "spot")
    bid = float(ticker.get("bid1Price") or ticker.get("lastPrice") or 0.0)
    qty = float(pos["quantity"])
    fee_rate = float(os.getenv("V2_FEE_RATE", "0.001"))
    mark_value = qty * bid
    hypothetical_fee = mark_value * fee_rate
    state["unrealized_pnl_usdt"] = (
        mark_value
        - hypothetical_fee
        - float(pos["entry_notional_usdt"])
        - float(pos["entry_fee_usdt"])
    )


def _maybe_open(provider, store, state):
    if state.get("open_position") is not None:
        return

    row = store.get_runtime("v2_scan")
    scan = None if row is None else row.get("value")
    if not isinstance(scan, dict):
        return

    realized_today, trades_today = _today_realized(state)
    daily_stop = float(os.getenv("V2_DAILY_STOP_USDT", "0.50"))
    max_trades_day = int(os.getenv("V2_MAX_TRADES_PER_DAY", "2"))
    if realized_today <= -daily_stop:
        state["last_rejection"] = {
            "time": str(_now()),
            "reason": "daily_loss_stop",
            "realized_today_usdt": realized_today,
        }
        return
    if trades_today >= max_trades_day:
        state["last_rejection"] = {
            "time": str(_now()),
            "reason": "daily_trade_limit",
            "trades_today": trades_today,
        }
        return

    seen = set(state.get("seen_signal_keys") or [])
    candidates = [
        x for x in (scan.get("candidates") or [])
        if x.get("action") == "SHADOW_READY"
        and float(x.get("score") or 0.0) >= float(os.getenv("V2_SHADOW_MIN_SCORE", "70"))
    ]
    if not candidates:
        return

    candidate = candidates[0]
    key = _signal_key(scan, candidate)
    if key in seen:
        return

    cooldown_minutes = int(os.getenv("V2_SETUP_COOLDOWN_MINUTES", "60"))
    setup_key = f"{candidate.get('symbol')}:{candidate.get('setup')}"
    last_entries = state.get("last_entry_by_setup") or {}
    last_raw = last_entries.get(setup_key)
    if last_raw:
        try:
            last_ts = pd.Timestamp(last_raw)
            if last_ts.tzinfo is None:
                last_ts = last_ts.tz_localize("UTC")
            if (_now() - last_ts).total_seconds() < cooldown_minutes * 60:
                state["last_rejection"] = {
                    "time": str(_now()),
                    "reason": "setup_cooldown",
                    "setup_key": setup_key,
                }
                seen.add(key)
                state["seen_signal_keys"] = list(seen)[-500:]
                return
        except Exception:
            pass
    seen.add(key)
    state["seen_signal_keys"] = list(seen)[-500:]

    risk = candidate.get("risk") or {}
    micro = candidate.get("microstructure") or {}
    if not risk.get("allowed"):
        state["last_rejection"] = {"time": str(_now()), "key": key, "reason": "risk_blocked"}
        return

    best_ask = float(micro.get("best_ask") or 0.0)
    notional = float(risk.get("notional_usdt") or 0.0)
    fee_rate = float(os.getenv("V2_FEE_RATE", "0.001"))
    entry_slip = float(os.getenv("V2_ENTRY_SLIPPAGE_PCT", "0.05"))
    entry_price = best_ask * (1.0 + entry_slip / 100.0)
    if entry_price <= 0 or notional <= 0:
        state["last_rejection"] = {"time": str(_now()), "key": key, "reason": "invalid_entry"}
        return

    entry_fee = notional * fee_rate
    total_cost = notional + entry_fee
    if total_cost > float(state["cash_usdt"]) + 1e-9:
        state["last_rejection"] = {"time": str(_now()), "key": key, "reason": "shadow_cash_insufficient"}
        return

    qty = notional / entry_price
    stop_pct = float(candidate["stop_pct"])
    target_pct = float(candidate["target_pct"])
    state["cash_usdt"] = float(state["cash_usdt"]) - total_cost
    state["fees_usdt"] = float(state.get("fees_usdt") or 0.0) + entry_fee
    state["open_position"] = {
        "signal_key": key,
        "symbol": candidate["symbol"],
        "setup": candidate["setup"],
        "score": float(candidate["score"]),
        "regime": candidate["regime"],
        "entry_time": str(_now()),
        "entry_price": entry_price,
        "entry_notional_usdt": notional,
        "entry_fee_usdt": entry_fee,
        "quantity": qty,
        "stop_pct": stop_pct,
        "target_pct": target_pct,
        "stop_price": entry_price * (1.0 - stop_pct / 100.0),
        "target_price": entry_price * (1.0 + target_pct / 100.0),
        "features": candidate.get("features"),
        "perp_features": candidate.get("perp_features"),
        "microstructure": micro,
    }
    state.setdefault("last_entry_by_setup", {})[setup_key] = str(_now())
    state["last_action"] = {
        "time": str(_now()),
        "action": "SHADOW_OPEN",
        "symbol": candidate["symbol"],
        "setup": candidate["setup"],
        "entry_price": entry_price,
        "notional_usdt": notional,
    }


def summary(state):
    start = float(state.get("starting_equity_usdt") or 15.0)
    metrics = trade_metrics(state.get("trades") or [], starting_equity=start)
    mc = monte_carlo(state.get("trades") or [], starting_equity=start, simulations=1000) if metrics["n"] >= 5 else None
    return {
        "mode": "V2_LIVE_SHADOW_1M_PATH",
        "started_at": state.get("started_at"),
        "starting_equity_usdt": round(start, 8),
        "current_equity_usdt": round(float(state.get("equity_usdt") or start), 8),
        "net_pnl_usdt": round(float(state.get("equity_usdt") or start) - start, 8),
        "realized_pnl_usdt": round(float(state.get("realized_pnl_usdt") or 0.0), 8),
        "unrealized_pnl_usdt": round(float(state.get("unrealized_pnl_usdt") or 0.0), 8),
        "fees_usdt": round(float(state.get("fees_usdt") or 0.0), 8),
        "open_position": state.get("open_position"),
        "closed_trades": len(state.get("trades") or []),
        "metrics": metrics,
        "monte_carlo": mc,
        "last_action": state.get("last_action"),
        "last_rejection": state.get("last_rejection"),
        "recent_trades": (state.get("trades") or [])[-20:],
        "last_updated_at": state.get("last_updated_at"),
    }


def process_v2_shadow(store, provider=None):
    provider = provider or BybitV2Provider()
    state = _load(store)

    _update_open_position(provider, state)
    _maybe_open(provider, store, state)

    open_value = 0.0
    pos = state.get("open_position")
    if pos:
        ticker = provider.ticker(pos["symbol"], "spot")
        bid = float(ticker.get("bid1Price") or ticker.get("lastPrice") or 0.0)
        open_value = float(pos["quantity"]) * bid
        fee_rate = float(os.getenv("V2_FEE_RATE", "0.001"))
        state["unrealized_pnl_usdt"] = (
            open_value
            - open_value * fee_rate
            - float(pos["entry_notional_usdt"])
            - float(pos["entry_fee_usdt"])
        )

    state["equity_usdt"] = float(state["cash_usdt"]) + open_value
    _save(store, state)
    return summary(state)
