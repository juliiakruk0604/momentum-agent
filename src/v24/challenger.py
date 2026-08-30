from __future__ import annotations

import os
import time
from dataclasses import dataclass


def _now_ms():
    return int(time.time() * 1000)


@dataclass
class EventDecision:
    ready: bool
    blockers: list[str]
    score: float
    regime: str


def event_gate(feature: dict, regime: str):
    blockers = []
    data_regime = str(regime)
    if data_regime in ("UNKNOWN", "DATA_DEGRADED"):
        blockers.append("market_data_not_ready")
    score = float(feature.get("microstructure_score") or 0.0)
    spread = float(feature.get("spread_pct") or 999.0)
    depth = min(
        float(feature.get("bid_depth_5_usdt") or 0.0),
        float(feature.get("ask_depth_5_usdt") or 0.0),
    )
    t1 = feature.get("trade_1s") or {}
    t5 = feature.get("trade_5s") or {}

    if str(regime).startswith("TREND_DOWN"):
        blockers.append("market_trend_down")
    if score < float(os.getenv("V24_ARM_SCORE", "78")):
        blockers.append("score_low")
    if float(feature.get("flow_confidence_1s") or 0.0) < float(os.getenv("V24_MIN_FLOW_CONF_1S", "0.55")):
        blockers.append("flow_confidence_1s_low")
    if float(feature.get("flow_confidence_5s") or 0.0) < float(os.getenv("V24_MIN_FLOW_CONF_5S", "0.65")):
        blockers.append("flow_confidence_5s_low")
    if float(t1.get("buy_ratio") or 0.5) < float(os.getenv("V24_MIN_BUY_RATIO_1S", "0.60")):
        blockers.append("buy_ratio_1s_low")
    if float(t5.get("buy_ratio") or 0.5) < float(os.getenv("V24_MIN_BUY_RATIO_5S", "0.56")):
        blockers.append("buy_ratio_5s_low")
    if float(t1.get("total_notional") or 0.0) < float(os.getenv("V24_MIN_NOTIONAL_1S", "1500")):
        blockers.append("trade_notional_1s_low")
    if spread > float(os.getenv("V24_MAX_SPREAD_PCT", "0.08")):
        blockers.append("spread_wide")
    if depth < float(os.getenv("V24_MIN_DEPTH_USDT", "5000")):
        blockers.append("depth_low")
    if float(feature.get("price_move_1s_pct") or 0.0) < float(os.getenv("V24_MIN_PRICE_MOVE_1S", "-0.03")):
        blockers.append("price_not_holding")
    if float(feature.get("ask_depletion_1s") or 0.0) < float(os.getenv("V24_MIN_ASK_DEPLETION", "-0.10")):
        blockers.append("ask_replenishing")
    return EventDecision(len(blockers) == 0, blockers, score, str(regime))


def confirmation_gate(armed: dict, feature: dict, regime: str, now_ms=None):
    now_ms = int(now_ms or _now_ms())
    blockers = []
    age_ms = now_ms - int(armed.get("armed_at_ms") or now_ms)
    min_ms = int(float(os.getenv("V24_CONFIRM_MIN_SECONDS", "2.0")) * 1000)
    max_ms = int(float(os.getenv("V24_CONFIRM_MAX_SECONDS", "8.0")) * 1000)

    if age_ms < min_ms:
        blockers.append("too_early")
    if age_ms > max_ms:
        blockers.append("expired")
    if feature.get("symbol") != armed.get("symbol"):
        blockers.append("symbol_changed")

    decision = event_gate(feature, regime)
    blockers.extend(decision.blockers)

    armed_score = float(armed.get("score") or 0.0)
    score = float(feature.get("microstructure_score") or 0.0)
    if armed_score > 0 and score < armed_score * float(os.getenv("V24_MIN_SCORE_RETENTION", "0.82")):
        blockers.append("score_collapsed")

    arm_mid = float(armed.get("mid") or 0.0)
    mid = float(feature.get("mid") or 0.0)
    if arm_mid > 0 and mid > 0:
        continuation = (mid / arm_mid - 1.0) * 100.0
        if continuation < float(os.getenv("V24_MIN_CONTINUATION_PCT", "-0.04")):
            blockers.append("price_failed")
        if continuation > float(os.getenv("V24_MAX_EXTENSION_PCT", "0.80")):
            blockers.append("too_extended")
    else:
        continuation = None

    if float(feature.get("book_imbalance_delta_1s") or 0.0) < float(os.getenv("V24_MIN_IMBALANCE_DELTA", "-0.12")):
        blockers.append("book_deteriorating")

    return {
        "confirmed": len(blockers) == 0,
        "blockers": list(dict.fromkeys(blockers)),
        "age_ms": age_ms,
        "continuation_pct": continuation,
        "score": score,
        "armed_score": armed_score,
    }


class V24EventShadow:
    KEY = "v24_event_shadow"

    def __init__(self, store):
        self.store = store
        self.state = self._load()

    def _new(self):
        start = float(os.getenv("V24_SHADOW_START_EQUITY_USDT", "15"))
        return {
            "strategy_version": "2.4",
            "started_at_ms": _now_ms(),
            "starting_equity_usdt": start,
            "cash_usdt": start,
            "equity_usdt": start,
            "armed": None,
            "open_position": None,
            "trades": [],
            "realized_pnl_usdt": 0.0,
            "last_action": None,
            "last_rejection": None,
            "live_execution": False,
        }

    def _load(self):
        row = self.store.get_runtime(self.KEY)
        if row is None or not isinstance(row.get("value"), dict):
            state = self._new()
            self.store.set_runtime(self.KEY, state)
            return state
        return row["value"]

    def _save(self):
        self.state["last_updated_at_ms"] = _now_ms()
        self.store.set_runtime(self.KEY, self.state)

    def _today_count(self):
        now_day = time.strftime("%Y-%m-%d", time.gmtime())
        count = 0
        pnl = 0.0
        for t in self.state.get("trades") or []:
            raw = int(t.get("exit_time_ms") or 0)
            day = time.strftime("%Y-%m-%d", time.gmtime(raw / 1000.0)) if raw else ""
            if day == now_day:
                count += 1
                pnl += float(t.get("pnl_usdt") or 0.0)
        return count, pnl

    def _mark_position(self, feature):
        pos = self.state.get("open_position")
        if not pos or feature.get("symbol") != pos.get("symbol"):
            return

        bid = float(feature.get("best_bid") or 0.0)
        if bid <= 0:
            return

        fee_rate = float(os.getenv("V2_FEE_RATE", "0.001"))
        exit_slip = float(os.getenv("V24_EXIT_SLIPPAGE_PCT", "0.03"))
        qty = float(pos["remaining_qty"])
        highest = max(float(pos.get("highest_bid") or bid), bid)
        pos["highest_bid"] = highest

        if pos.get("partial_hit"):
            trail = highest * (1.0 - float(os.getenv("V24_TRAIL_PCT", "0.80")) / 100.0)
            breakeven = float(pos["entry_price"]) * (1.0 + float(os.getenv("V24_BREAKEVEN_BUFFER_PCT", "0.25")) / 100.0)
            pos["active_stop"] = max(float(pos["active_stop"]), trail, breakeven)

        exit_reason = None
        if bid <= float(pos["active_stop"]):
            exit_reason = "STOP" if not pos.get("partial_hit") else "EVENT_TRAIL"

        if not pos.get("partial_hit") and bid >= float(pos["partial_target"]):
            fraction = float(os.getenv("V24_PARTIAL_FRACTION", "0.5"))
            partial_qty = float(pos["initial_qty"]) * fraction
            px = bid * (1.0 - exit_slip / 100.0)
            proceeds = partial_qty * px
            fee = proceeds * fee_rate
            self.state["cash_usdt"] = float(self.state["cash_usdt"]) + proceeds - fee
            pos["remaining_qty"] = max(0.0, float(pos["remaining_qty"]) - partial_qty)
            pos["partial_hit"] = True
            pos["partial_exit_price"] = px
            pos["partial_exit_time_ms"] = _now_ms()
            pos["partial_fee_usdt"] = fee
            pos["active_stop"] = max(
                float(pos["active_stop"]),
                float(pos["entry_price"]) * (1.0 + float(os.getenv("V24_BREAKEVEN_BUFFER_PCT", "0.25")) / 100.0),
            )

        if exit_reason is not None:
            px = bid * (1.0 - exit_slip / 100.0)
            proceeds = float(pos["remaining_qty"]) * px
            fee = proceeds * fee_rate
            self.state["cash_usdt"] = float(self.state["cash_usdt"]) + proceeds - fee
            pnl = float(self.state["cash_usdt"]) - float(pos["equity_before_entry"])
            trade = {
                **pos,
                "exit_time_ms": _now_ms(),
                "exit_price": px,
                "exit_reason": exit_reason,
                "exit_fee_usdt": fee,
                "pnl_usdt": pnl,
            }
            self.state["trades"] = (self.state.get("trades") or []) + [trade]
            self.state["trades"] = self.state["trades"][-250:]
            self.state["realized_pnl_usdt"] = float(self.state.get("realized_pnl_usdt") or 0.0) + pnl
            self.state["open_position"] = None
            self.state["equity_usdt"] = float(self.state["cash_usdt"])
            self.state["last_action"] = {
                "action": "V24_CLOSE",
                "symbol": trade["symbol"],
                "pnl_usdt": pnl,
                "reason": exit_reason,
                "time_ms": _now_ms(),
            }
            return

        mark_px = bid * (1.0 - exit_slip / 100.0)
        mark_value = float(pos["remaining_qty"]) * mark_px
        mark_fee = mark_value * fee_rate
        self.state["equity_usdt"] = float(self.state["cash_usdt"]) + mark_value - mark_fee

    def _try_arm_or_enter(self, ranked_features, regime):
        if self.state.get("open_position") is not None:
            return

        trades_today, pnl_today = self._today_count()
        if trades_today >= int(os.getenv("V24_MAX_TRADES_PER_DAY", "3")):
            return
        if pnl_today <= -float(os.getenv("V24_DAILY_STOP_USDT", "0.40")):
            return

        armed = self.state.get("armed")
        if armed is None:
            for feature in ranked_features:
                d = event_gate(feature, regime)
                if d.ready:
                    self.state["armed"] = {
                        "symbol": feature["symbol"],
                        "armed_at_ms": _now_ms(),
                        "score": d.score,
                        "mid": float(feature.get("mid") or 0.0),
                        "regime": regime,
                    }
                    self.state["last_action"] = {
                        "action": "V24_ARM",
                        "symbol": feature["symbol"],
                        "score": d.score,
                        "time_ms": _now_ms(),
                    }
                    return
            return

        feature = next((f for f in ranked_features if f.get("symbol") == armed.get("symbol")), None)
        if feature is None:
            self.state["last_rejection"] = {
                "reason": "armed_symbol_missing",
                "symbol": armed.get("symbol"),
                "time_ms": _now_ms(),
            }
            self.state["armed"] = None
            return

        decision = confirmation_gate(armed, feature, regime)
        if not decision["confirmed"]:
            if "expired" in decision["blockers"] or "price_failed" in decision["blockers"] or "market_trend_down" in decision["blockers"]:
                self.state["last_rejection"] = {
                    "reason": "confirmation_failed",
                    "symbol": armed.get("symbol"),
                    "details": decision,
                    "time_ms": _now_ms(),
                }
                self.state["armed"] = None
            return

        ask = float(feature.get("best_ask") or 0.0)
        if ask <= 0:
            self.state["armed"] = None
            return
        notional = min(
            float(os.getenv("V24_MAX_NOTIONAL_USDT", "5")),
            max(0.0, float(self.state["equity_usdt"]) * 0.34),
        )
        if notional < 5.0:
            self.state["last_rejection"] = {"reason": "below_exchange_minimum", "time_ms": _now_ms()}
            self.state["armed"] = None
            return

        fee_rate = float(os.getenv("V2_FEE_RATE", "0.001"))
        entry_slip = float(os.getenv("V24_ENTRY_SLIPPAGE_PCT", "0.03"))
        entry = ask * (1.0 + entry_slip / 100.0)
        fee = notional * fee_rate
        total = notional + fee
        if total > float(self.state["cash_usdt"]):
            self.state["last_rejection"] = {"reason": "insufficient_shadow_cash", "time_ms": _now_ms()}
            self.state["armed"] = None
            return

        stop_pct = float(os.getenv("V24_INITIAL_STOP_PCT", "0.70"))
        risk_distance = entry * stop_pct / 100.0
        partial_r = float(os.getenv("V24_PARTIAL_R", "1.5"))
        qty = notional / entry
        equity_before = float(self.state["equity_usdt"])
        self.state["cash_usdt"] = float(self.state["cash_usdt"]) - total
        self.state["open_position"] = {
            "strategy_version": "2.4",
            "symbol": feature["symbol"],
            "entry_time_ms": _now_ms(),
            "entry_price": entry,
            "entry_notional_usdt": notional,
            "entry_fee_usdt": fee,
            "initial_qty": qty,
            "remaining_qty": qty,
            "equity_before_entry": equity_before,
            "initial_stop_pct": stop_pct,
            "active_stop": entry - risk_distance,
            "partial_target": entry + partial_r * risk_distance,
            "partial_hit": False,
            "highest_bid": float(feature.get("best_bid") or entry),
            "confirmation": decision,
            "entry_feature": feature,
        }
        self.state["armed"] = None
        self.state["last_action"] = {
            "action": "V24_OPEN",
            "symbol": feature["symbol"],
            "entry_price": entry,
            "notional_usdt": notional,
            "time_ms": _now_ms(),
        }

    def process(self, ranked_features, regime):
        pos = self.state.get("open_position")
        if pos:
            feature = next((f for f in ranked_features if f.get("symbol") == pos.get("symbol")), None)
            if feature is not None:
                self._mark_position(feature)
        self._try_arm_or_enter(ranked_features, regime)
        self._save()
        return self.summary()

    def summary(self):
        start = float(self.state.get("starting_equity_usdt") or 15.0)
        return {
            "mode": "V2.4_EVENT_DRIVEN_SHADOW",
            "strategy_version": "2.4",
            "starting_equity_usdt": start,
            "current_equity_usdt": round(float(self.state.get("equity_usdt") or start), 8),
            "net_pnl_usdt": round(float(self.state.get("equity_usdt") or start) - start, 8),
            "realized_pnl_usdt": round(float(self.state.get("realized_pnl_usdt") or 0.0), 8),
            "armed": self.state.get("armed"),
            "open_position": self.state.get("open_position"),
            "closed_trades": len(self.state.get("trades") or []),
            "last_action": self.state.get("last_action"),
            "last_rejection": self.state.get("last_rejection"),
            "recent_trades": (self.state.get("trades") or [])[-20:],
            "live_execution": False,
        }
