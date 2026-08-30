from __future__ import annotations

import os
from datetime import datetime, timezone

import pandas as pd

from src.execution.bybit_spot import build_spot_plan, spot_ticker


KEY = "shadow_portfolio_v1"


def _now():
    return pd.Timestamp.now(tz="UTC")


def _fresh_candidates(store):
    now = _now()
    out = []
    for c in store.confirmed_candidates(limit=50):
        readiness = c.get("readiness") or {}
        derivatives = c.get("derivatives") or {}
        if readiness.get("state") != "EARLY ENTRY":
            continue
        try:
            signal_time = pd.Timestamp(c.get("signal_time"))
            if signal_time.tzinfo is None:
                signal_time = signal_time.tz_localize("UTC")
            age_min = (now - signal_time).total_seconds() / 60.0
        except Exception:
            continue
        if age_min < 0 or age_min > 90:
            continue

        oi_change = derivatives.get("oi_change_1h_pct")
        funding = derivatives.get("funding_rate")
        if oi_change is None or float(oi_change) < 2.0:
            continue
        if funding is not None and abs(float(funding)) > 0.0015:
            continue

        out.append({
            "id": c.get("id"),
            "symbol": c.get("symbol"),
            "signal_time": c.get("signal_time"),
            "signal_price": float(c.get("signal_price") or 0.0),
            "final_score": float(readiness.get("final_score") or 0.0),
            "oi_change_1h_pct": float(oi_change),
            "funding_rate": None if funding is None else float(funding),
            "age_minutes": round(age_min, 1),
        })
    out.sort(key=lambda x: (x["final_score"], x["oi_change_1h_pct"]), reverse=True)
    return out


def _baseline_prices():
    out = {}
    for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        try:
            t = spot_ticker(symbol)
            out[symbol] = float(t.get("lastPrice") or t.get("bid1Price") or t.get("ask1Price") or 0.0)
        except Exception:
            out[symbol] = None
    return out


def _new_state():
    starting = float(os.getenv("SHADOW_START_EQUITY_USDT", "15"))
    return {
        "version": 1,
        "started_at": str(_now()),
        "starting_equity_usdt": starting,
        "cash_usdt": starting,
        "equity_usdt": starting,
        "realized_pnl_usdt": 0.0,
        "unrealized_pnl_usdt": 0.0,
        "fees_usdt": 0.0,
        "open_position": None,
        "trades": [],
        "seen_signal_ids": [],
        "baseline_start_prices": _baseline_prices(),
        "last_mark_prices": {},
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


def _close_position(state, exit_price, reason):
    pos = state.get("open_position")
    if not pos:
        return state

    qty = float(pos["quantity"])
    entry_price = float(pos["entry_price"])
    entry_notional = float(pos["entry_notional_usdt"])
    fee_rate = float(os.getenv("SHADOW_FEE_RATE", "0.001"))

    gross_exit = qty * float(exit_price)
    exit_fee = gross_exit * fee_rate
    net_exit = gross_exit - exit_fee
    realized = net_exit - entry_notional - float(pos["entry_fee_usdt"])

    state["cash_usdt"] = float(state["cash_usdt"]) + net_exit
    state["realized_pnl_usdt"] = float(state["realized_pnl_usdt"]) + realized
    state["fees_usdt"] = float(state["fees_usdt"]) + exit_fee

    trade = {
        **pos,
        "exit_time": str(_now()),
        "exit_price": float(exit_price),
        "exit_reason": reason,
        "exit_fee_usdt": round(exit_fee, 8),
        "realized_pnl_usdt": round(realized, 8),
        "return_pct_on_position": round((realized / entry_notional * 100.0) if entry_notional > 0 else 0.0, 4),
    }
    state["trades"] = (state.get("trades") or []) + [trade]
    state["trades"] = state["trades"][-200:]
    state["open_position"] = None
    state["unrealized_pnl_usdt"] = 0.0
    return state


def process_shadow_portfolio(store):
    state = _load(store)
    fee_rate = float(os.getenv("SHADOW_FEE_RATE", "0.001"))
    exit_slip_pct = float(os.getenv("SHADOW_EXIT_SLIPPAGE_PCT", "0.05"))
    max_hold_hours = float(os.getenv("SHADOW_MAX_HOLD_HOURS", "12"))

    # Mark/exit open position first.
    pos = state.get("open_position")
    if pos:
        symbol = pos["symbol"]
        t = spot_ticker(symbol)
        bid = float(t.get("bid1Price") or t.get("lastPrice") or 0.0)
        if bid > 0:
            state["last_mark_prices"][symbol] = bid
            qty = float(pos["quantity"])
            mark_value = qty * bid
            entry_cost = float(pos["entry_notional_usdt"]) + float(pos["entry_fee_usdt"])
            hypothetical_exit_fee = mark_value * fee_rate
            state["unrealized_pnl_usdt"] = mark_value - hypothetical_exit_fee - entry_cost

            stop_price = float(pos["stop_price"])
            tp_price = float(pos["take_profit_price"])
            entry_time = pd.Timestamp(pos["entry_time"])
            if entry_time.tzinfo is None:
                entry_time = entry_time.tz_localize("UTC")
            held_hours = (_now() - entry_time).total_seconds() / 3600.0

            if bid <= stop_price:
                conservative = min(bid, stop_price) * (1.0 - exit_slip_pct / 100.0)
                state = _close_position(state, conservative, "STOP")
            elif bid >= tp_price:
                conservative = tp_price * (1.0 - exit_slip_pct / 100.0)
                state = _close_position(state, conservative, "TAKE_PROFIT")
            elif held_hours >= max_hold_hours:
                conservative = bid * (1.0 - exit_slip_pct / 100.0)
                state = _close_position(state, conservative, "TIME_EXIT")

    # Open a new shadow trade only if flat.
    if state.get("open_position") is None:
        seen = set(state.get("seen_signal_ids") or [])
        candidates = [x for x in _fresh_candidates(store) if x.get("id") not in seen]
        if candidates:
            candidate = candidates[0]
            try:
                # Keep the exact same $5 notional/risk preflight, but simulated balance only.
                original_cap = os.getenv("LIVE_MAX_NOTIONAL_USDT")
                os.environ["LIVE_MAX_NOTIONAL_USDT"] = os.getenv("SHADOW_MAX_NOTIONAL_USDT", "5")
                try:
                    plan = build_spot_plan(
                        candidate["symbol"],
                        candidate["signal_price"],
                        allow_transfer=False,
                        simulated_balance_usdt=float(state["cash_usdt"]),
                    )
                finally:
                    if original_cap is None:
                        os.environ.pop("LIVE_MAX_NOTIONAL_USDT", None)
                    else:
                        os.environ["LIVE_MAX_NOTIONAL_USDT"] = original_cap
            except Exception as exc:
                state["seen_signal_ids"] = list(seen | {candidate["id"]})[-500:]
                state["last_rejection"] = {
                    "time": str(_now()),
                    "candidate": candidate,
                    "reason": "preflight_exception",
                    "error": repr(exc),
                }
            else:
                state["seen_signal_ids"] = list(seen | {candidate["id"]})[-500:]
                if plan.allowed:
                    entry_price = float(plan.limit_price)
                    qty = float(plan.quantity)
                    entry_notional = qty * entry_price
                    entry_fee = entry_notional * fee_rate
                    total_cost = entry_notional + entry_fee
                    if total_cost <= float(state["cash_usdt"]) + 1e-9:
                        state["cash_usdt"] = float(state["cash_usdt"]) - total_cost
                        state["fees_usdt"] = float(state["fees_usdt"]) + entry_fee
                        state["open_position"] = {
                            "signal_id": candidate["id"],
                            "symbol": candidate["symbol"],
                            "signal_time": candidate["signal_time"],
                            "entry_time": str(_now()),
                            "signal_price": candidate["signal_price"],
                            "entry_price": entry_price,
                            "quantity": qty,
                            "entry_notional_usdt": round(entry_notional, 8),
                            "entry_fee_usdt": round(entry_fee, 8),
                            "stop_price": float(plan.stop_price),
                            "take_profit_price": float(plan.take_profit_price),
                            "spread_pct": float(plan.spread_pct),
                            "final_score": candidate["final_score"],
                            "oi_change_1h_pct": candidate["oi_change_1h_pct"],
                            "funding_rate": candidate["funding_rate"],
                        }
                        state["last_action"] = {
                            "time": str(_now()),
                            "action": "SHADOW_BUY",
                            "symbol": candidate["symbol"],
                            "entry_price": entry_price,
                        }
                    else:
                        state["last_rejection"] = {
                            "time": str(_now()),
                            "candidate": candidate,
                            "reason": "shadow_cash_insufficient_after_fees",
                        }
                else:
                    state["last_rejection"] = {
                        "time": str(_now()),
                        "candidate": candidate,
                        "reason": "preflight_blocked",
                        "blockers": plan.blockers,
                    }

    # Recompute mark-to-market equity.
    open_value = 0.0
    open_pos = state.get("open_position")
    if open_pos:
        try:
            t = spot_ticker(open_pos["symbol"])
            bid = float(t.get("bid1Price") or t.get("lastPrice") or 0.0)
            state["last_mark_prices"][open_pos["symbol"]] = bid
            open_value = float(open_pos["quantity"]) * bid
            hypothetical_exit_fee = open_value * fee_rate
            state["unrealized_pnl_usdt"] = (
                open_value
                - hypothetical_exit_fee
                - float(open_pos["entry_notional_usdt"])
                - float(open_pos["entry_fee_usdt"])
            )
        except Exception:
            pass

    state["equity_usdt"] = float(state["cash_usdt"]) + open_value
    _save(store, state)
    return summary(state)


def summary(state):
    trades = state.get("trades") or []
    wins = [t for t in trades if float(t.get("realized_pnl_usdt") or 0.0) > 0]
    losses = [t for t in trades if float(t.get("realized_pnl_usdt") or 0.0) < 0]
    starting = float(state.get("starting_equity_usdt") or 0.0)
    equity = float(state.get("equity_usdt") or 0.0)

    baseline_returns = {}
    for symbol, start_price in (state.get("baseline_start_prices") or {}).items():
        if not start_price:
            baseline_returns[symbol] = None
            continue
        try:
            current = float(spot_ticker(symbol).get("lastPrice") or 0.0)
            baseline_returns[symbol] = round((current / float(start_price) - 1.0) * 100.0, 4)
        except Exception:
            baseline_returns[symbol] = None

    return {
        "mode": "LIVE_SHADOW_REAL_MARKET",
        "started_at": state.get("started_at"),
        "starting_equity_usdt": round(starting, 8),
        "current_equity_usdt": round(equity, 8),
        "net_pnl_usdt": round(equity - starting, 8),
        "net_return_pct": round(((equity / starting - 1.0) * 100.0) if starting > 0 else 0.0, 4),
        "realized_pnl_usdt": round(float(state.get("realized_pnl_usdt") or 0.0), 8),
        "unrealized_pnl_usdt": round(float(state.get("unrealized_pnl_usdt") or 0.0), 8),
        "fees_usdt": round(float(state.get("fees_usdt") or 0.0), 8),
        "closed_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round((len(wins) / len(trades) * 100.0) if trades else 0.0, 2),
        "open_position": state.get("open_position"),
        "last_rejection": state.get("last_rejection"),
        "last_action": state.get("last_action"),
        "baseline_buy_hold_return_pct": baseline_returns,
        "recent_trades": trades[-20:],
        "last_updated_at": state.get("last_updated_at"),
    }
