from __future__ import annotations

import os
import time


def _now_ms():
    return int(time.time() * 1000)


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return float(default)


def _base_gate(feature: dict, regime: str):
    blockers = []
    base = feature.get("base_momentum") or {}
    ff = base.get("fast_features") or {}

    if not base:
        blockers.append("no_base_momentum")
        return blockers

    if str(regime).startswith("TREND_DOWN"):
        blockers.append("market_trend_down")

    score = _safe_float(base.get("score"))
    rs5 = _safe_float(ff.get("rs_5m_pct"))
    ret3 = _safe_float(ff.get("ret_3m_pct"))
    volacc = _safe_float(ff.get("volume_acceleration"))
    cur = _safe_float(ff.get("current_move_pct"))
    price_accel = _safe_float(ff.get("price_acceleration"))

    if score < float(os.getenv("V25_MIN_BASE_SCORE", "72")):
        blockers.append("base_score_low")
    if ret3 <= float(os.getenv("V25_MIN_RET_3M_PCT", "0.05")):
        blockers.append("base_3m_momentum_low")
    if rs5 < float(os.getenv("V25_MIN_RS_5M_PCT", "0.10")):
        blockers.append("relative_strength_low")
    if volacc < float(os.getenv("V25_MIN_VOLUME_ACCEL", "1.15")):
        blockers.append("volume_acceleration_low")
    if price_accel < float(os.getenv("V25_MIN_PRICE_ACCEL", "-0.02")):
        blockers.append("price_acceleration_low")
    if cur > float(os.getenv("V25_MAX_CURRENT_EXTENSION_PCT", "0.65")):
        blockers.append("base_too_extended")

    return blockers


def _micro_gate(feature: dict):
    blockers = []
    seq = feature.get("sequence_context") or {}
    t1 = feature.get("trade_1s") or {}
    t5 = feature.get("trade_5s") or {}
    cross = feature.get("cross_exchange") or {}
    perp = feature.get("perp_context") or {}

    score = _safe_float(feature.get("microstructure_score"))
    spread = _safe_float(feature.get("spread_pct"), 999.0)
    depth = min(
        _safe_float(feature.get("bid_depth_5_usdt")),
        _safe_float(feature.get("ask_depth_5_usdt")),
    )

    if score < float(os.getenv("V25_MIN_MICRO_SCORE", "66")):
        blockers.append("micro_score_low")
    if _safe_float(seq.get("score_persistence_3s")) < float(os.getenv("V25_MIN_SCORE_PERSISTENCE_3S", "0.50")):
        blockers.append("micro_not_persistent")
    if _safe_float(seq.get("buy_ratio_mean_3s"), 0.5) < float(os.getenv("V25_MIN_BUY_RATIO_MEAN_3S", "0.54")):
        blockers.append("buy_flow_low")
    if _safe_float(seq.get("book_imbalance_mean_3s")) < float(os.getenv("V25_MIN_BOOK_IMBALANCE_MEAN_3S", "-0.05")):
        blockers.append("book_imbalance_low")
    if _safe_float(t5.get("buy_ratio"), 0.5) < float(os.getenv("V25_MIN_BUY_RATIO_5S", "0.53")):
        blockers.append("buy_ratio_5s_low")
    if spread > float(os.getenv("V25_MAX_SPREAD_PCT", "0.08")):
        blockers.append("spread_wide")
    if depth < float(os.getenv("V25_MIN_DEPTH_USDT", "2500")):
        blockers.append("depth_low")

    # External venues/perp are used as vetoes only, not positive alpha weights.
    ext = _safe_float(cross.get("external_minus_bybit_move_5s_pct"))
    if (cross.get("binance_available") or cross.get("okx_available")) and ext < float(
        os.getenv("V25_MIN_EXTERNAL_LEAD_5S_PCT", "-0.08")
    ):
        blockers.append("external_venues_lagging")

    oi = _safe_float(perp.get("oi_change_30s_pct"))
    if oi < float(os.getenv("V25_MIN_OI_CHANGE_30S_PCT", "-0.20")):
        blockers.append("oi_collapsing")

    return blockers


def hybrid_gate(feature: dict, regime: str):
    blockers = _base_gate(feature, regime) + _micro_gate(feature)
    return list(dict.fromkeys(blockers))


class V25HybridShadow:
    KEY = "v25_hybrid_shadow"

    def __init__(self, store):
        self.store = store
        row = store.get_runtime(self.KEY)
        self.state = row.get("value") if row and isinstance(row.get("value"), dict) else self._new()

    def _new(self):
        start = float(os.getenv("V25_SHADOW_START_EQUITY_USDT", "15"))
        return {
            "strategy_version": "2.5",
            "mode": "HYBRID_MOMENTUM_MICROSTRUCTURE",
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

    def _save(self):
        self.state["last_updated_at_ms"] = _now_ms()
        self.store.set_runtime(self.KEY, self.state)

    def _today_count(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        out = 0
        for trade in self.state.get("trades") or []:
            raw = int(trade.get("exit_time_ms") or 0)
            if raw and time.strftime("%Y-%m-%d", time.gmtime(raw / 1000.0)) == day:
                out += 1
        return out

    def _mark(self, feature):
        pos = self.state.get("open_position")
        if not pos or feature.get("symbol") != pos.get("symbol"):
            return

        bid = _safe_float(feature.get("best_bid"))
        if bid <= 0:
            return

        fee_rate = float(os.getenv("V2_FEE_RATE", "0.001"))
        exit_slip = float(os.getenv("V25_EXIT_SLIPPAGE_PCT", "0.03"))
        now = _now_ms()

        reason = None
        exit_level = None
        if bid <= _safe_float(pos.get("stop_price")):
            reason = "STOP"
            exit_level = bid
        elif bid >= _safe_float(pos.get("target_price")):
            reason = "TARGET"
            exit_level = _safe_float(pos.get("target_price"))
        elif now - int(pos.get("entry_time_ms") or now) >= int(
            float(os.getenv("V25_MAX_HOLD_MINUTES", "30")) * 60_000
        ):
            reason = "TIME_EXIT"
            exit_level = bid

        if reason is not None:
            px = float(exit_level) * (1.0 - exit_slip / 100.0)
            proceeds = _safe_float(pos.get("qty")) * px
            exit_fee = proceeds * fee_rate
            self.state["cash_usdt"] = _safe_float(self.state.get("cash_usdt")) + proceeds - exit_fee
            pnl = _safe_float(self.state.get("cash_usdt")) - _safe_float(pos.get("equity_before_entry"))
            trade = {
                **pos,
                "exit_time_ms": now,
                "exit_price": px,
                "exit_fee_usdt": exit_fee,
                "exit_reason": reason,
                "pnl_usdt": pnl,
            }
            self.state["trades"] = ((self.state.get("trades") or []) + [trade])[-250:]
            self.state["realized_pnl_usdt"] = _safe_float(self.state.get("realized_pnl_usdt")) + pnl
            self.state["open_position"] = None
            self.state["equity_usdt"] = _safe_float(self.state.get("cash_usdt"))
            self.state["last_action"] = {
                "action": "V25_CLOSE",
                "symbol": trade["symbol"],
                "reason": reason,
                "pnl_usdt": pnl,
                "time_ms": now,
            }
            return

        marked = bid * (1.0 - exit_slip / 100.0)
        value = _safe_float(pos.get("qty")) * marked
        hypothetical_exit_fee = value * fee_rate
        self.state["equity_usdt"] = _safe_float(self.state.get("cash_usdt")) + value - hypothetical_exit_fee

    def _try_open(self, ranked, regime):
        if self.state.get("open_position") is not None:
            return
        if self._today_count() >= int(os.getenv("V25_MAX_TRADES_PER_DAY", "3")):
            return

        armed = self.state.get("armed")
        if armed is None:
            for feature in ranked:
                blockers = hybrid_gate(feature, regime)
                if blockers:
                    continue
                self.state["armed"] = {
                    "symbol": feature.get("symbol"),
                    "armed_at_ms": _now_ms(),
                    "micro_score": _safe_float(feature.get("microstructure_score")),
                    "entry_ref_mid": _safe_float(feature.get("mid")),
                }
                self.state["last_action"] = {
                    "action": "V25_ARM",
                    "symbol": feature.get("symbol"),
                    "time_ms": _now_ms(),
                }
                return
            return

        feature = next((x for x in ranked if x.get("symbol") == armed.get("symbol")), None)
        if feature is None:
            self.state["armed"] = None
            return

        age = _now_ms() - int(armed.get("armed_at_ms") or _now_ms())
        if age < int(float(os.getenv("V25_CONFIRM_SECONDS", "3")) * 1000):
            return
        if age > int(float(os.getenv("V25_CONFIRM_TIMEOUT_SECONDS", "10")) * 1000):
            self.state["last_rejection"] = {"reason":"confirmation_timeout","symbol":armed.get("symbol"),"time_ms":_now_ms()}
            self.state["armed"] = None
            return

        blockers = hybrid_gate(feature, regime)
        if blockers:
            self.state["last_rejection"] = {
                "reason":"confirmation_failed",
                "symbol":armed.get("symbol"),
                "blockers":blockers,
                "time_ms":_now_ms(),
            }
            self.state["armed"] = None
            return

        ask = _safe_float(feature.get("best_ask"))
        if ask <= 0:
            self.state["armed"] = None
            return

        base = feature.get("base_momentum") or {}
        base_risk = base.get("risk") or {}
        stop_pct = max(
            float(os.getenv("V25_MIN_STOP_PCT", "0.55")),
            min(float(os.getenv("V25_MAX_STOP_PCT", "1.50")), _safe_float(base.get("initial_stop_pct"), 0.65)),
        )
        target_pct = max(
            float(os.getenv("V25_MIN_TARGET_PCT", "1.50")),
            _safe_float(base_risk.get("target_pct"), max(1.5, stop_pct * 2.25)),
        )

        entry_slip = float(os.getenv("V25_ENTRY_SLIPPAGE_PCT", "0.03"))
        fee_rate = float(os.getenv("V2_FEE_RATE", "0.001"))
        entry = ask * (1.0 + entry_slip / 100.0)
        notional = min(float(os.getenv("V25_MAX_NOTIONAL_USDT", "5")), _safe_float(self.state.get("equity_usdt")) * 0.34)
        if notional < 5.0:
            self.state["armed"] = None
            self.state["last_rejection"] = {"reason":"below_exchange_minimum","time_ms":_now_ms()}
            return

        entry_fee = notional * fee_rate
        total = notional + entry_fee
        if total > _safe_float(self.state.get("cash_usdt")):
            self.state["armed"] = None
            self.state["last_rejection"] = {"reason":"insufficient_shadow_cash","time_ms":_now_ms()}
            return

        qty = notional / entry
        equity_before = _safe_float(self.state.get("equity_usdt"))
        self.state["cash_usdt"] = _safe_float(self.state.get("cash_usdt")) - total
        self.state["open_position"] = {
            "strategy_version":"2.5",
            "symbol":feature.get("symbol"),
            "entry_time_ms":_now_ms(),
            "entry_price":entry,
            "entry_notional_usdt":notional,
            "entry_fee_usdt":entry_fee,
            "qty":qty,
            "equity_before_entry":equity_before,
            "stop_pct":stop_pct,
            "target_pct":target_pct,
            "stop_price":entry * (1.0 - stop_pct / 100.0),
            "target_price":entry * (1.0 + target_pct / 100.0),
            "entry_feature":feature,
            "base_signal":base,
        }
        self.state["armed"] = None
        self.state["last_action"] = {
            "action":"V25_OPEN",
            "symbol":feature.get("symbol"),
            "entry_price":entry,
            "stop_pct":stop_pct,
            "target_pct":target_pct,
            "time_ms":_now_ms(),
        }

    def process(self, ranked_features, regime):
        pos = self.state.get("open_position")
        if pos:
            feature = next((x for x in ranked_features if x.get("symbol") == pos.get("symbol")), None)
            if feature is not None:
                self._mark(feature)
        self._try_open(ranked_features, regime)
        self._save()
        return self.summary()

    def summary(self):
        start = _safe_float(self.state.get("starting_equity_usdt"), 15.0)
        equity = _safe_float(self.state.get("equity_usdt"), start)
        return {
            "mode":"V2.5_HYBRID_SHADOW",
            "strategy_version":"2.5",
            "starting_equity_usdt":start,
            "current_equity_usdt":round(equity, 8),
            "net_pnl_usdt":round(equity - start, 8),
            "realized_pnl_usdt":round(_safe_float(self.state.get("realized_pnl_usdt")), 8),
            "armed":self.state.get("armed"),
            "open_position":self.state.get("open_position"),
            "closed_trades":len(self.state.get("trades") or []),
            "last_action":self.state.get("last_action"),
            "last_rejection":self.state.get("last_rejection"),
            "recent_trades":(self.state.get("trades") or [])[-20:],
            "live_execution":False,
        }
