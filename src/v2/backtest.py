from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from .features import compute_features
from .provider import BybitV2Provider
from .regime import detect_regime
from .risk import cost_adjusted_levels
from .research import trade_metrics, monte_carlo, grouped_trade_metrics
from .setups import evaluate_setups
from .simulator import simulate_long_path


def _ms(ts):
    return int(pd.Timestamp(ts).timestamp() * 1000)


def _strategy_config_snapshot():
    keys = [
        "V2_BACKTEST_MIN_SCORE",
        "V2_SETUP_COOLDOWN_MINUTES",
        "V2_SHADOW_MAX_HOLD_MINUTES",
        "V2_FEE_RATE",
        "V2_BACKTEST_ENTRY_SLIPPAGE_PCT",
        "V2_EXIT_SLIPPAGE_PCT",
        "V2_BACKTEST_SPREAD_PCT",
        "V2_MIN_NET_RR",
        "V2_MAX_TARGET_PCT",
        "V2_MAX_NOTIONAL_USDT",
    ]
    return {key: os.getenv(key) for key in keys}


def _strategy_fingerprint():
    root = Path(__file__).resolve().parents[2]
    paths = [
        "src/v2/features.py",
        "src/v2/regime.py",
        "src/v2/setups.py",
        "src/v2/risk.py",
        "src/v2/simulator.py",
        "src/v2/backtest.py",
    ]
    digest = hashlib.sha256()
    for relative in paths:
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update((root / relative).read_bytes())
        digest.update(b"\0")
    digest.update(json.dumps(_strategy_config_snapshot(), sort_keys=True).encode("utf-8"))
    return digest.hexdigest()[:16]


def _dataset_id(start, end, universe):
    payload = json.dumps({
        "start": str(start),
        "end": str(end),
        "universe": list(universe),
        "version": 2,
        "strategy_fingerprint": _strategy_fingerprint(),
        "strategy_config": _strategy_config_snapshot(),
    }, sort_keys=True).encode("utf-8")
    return "v2_spot_" + hashlib.sha256(payload).hexdigest()[:16]


def _to_utc(ts):
    x = pd.Timestamp(ts)
    return x.tz_localize("UTC") if x.tzinfo is None else x


def _portfolio_filter(trades, starting_equity=15.0, max_trades_per_day=2, daily_stop_usdt=0.50):
    ordered = sorted(trades, key=lambda t: pd.Timestamp(t["entry_time"]))
    accepted = []
    open_until = None
    daily = {}

    for trade in ordered:
        entry = _to_utc(trade["entry_time"])
        exit_ts = _to_utc(trade["exit_time"])
        day = entry.date().isoformat()
        stats = daily.setdefault(day, {"pnl": 0.0, "count": 0})

        if open_until is not None and entry <= open_until:
            continue
        if stats["count"] >= int(max_trades_per_day):
            continue
        if stats["pnl"] <= -float(daily_stop_usdt):
            continue

        accepted.append(trade)
        stats["pnl"] += float(trade.get("pnl_usdt") or 0.0)
        stats["count"] += 1
        open_until = exit_ts

    return accepted


def _fold_metrics(trades, starting_equity=15.0, fold_days=7):
    if not trades:
        return []
    ordered = sorted(trades, key=lambda t: pd.Timestamp(t["entry_time"]))
    start = _to_utc(ordered[0]["entry_time"]).floor("D")
    end = _to_utc(ordered[-1]["entry_time"]) + pd.Timedelta(days=1)
    folds = []
    cursor = start
    fold_id = 0
    while cursor < end:
        nxt = cursor + pd.Timedelta(days=int(fold_days))
        subset = [
            t for t in ordered
            if cursor <= _to_utc(t["entry_time"]) < nxt
        ]
        metrics = trade_metrics(subset, starting_equity=starting_equity)
        folds.append({
            "fold_id": fold_id,
            "start": str(cursor),
            "end": str(nxt),
            "metrics": {k: v for k, v in metrics.items() if k != "equity_curve"},
        })
        cursor = nxt
        fold_id += 1
    return folds


class V2BacktestRunner:
    STATE_KEY = "v2_backtest_state"

    def __init__(self, store, provider=None):
        self.store = store
        self.provider = provider or BybitV2Provider()
        self._bench_key = None
        self._btc = None
        self._eth = None

    def _new_state(self):
        now = pd.Timestamp.now(tz="UTC")
        holdout_days = int(os.getenv("V2_BACKTEST_HOLDOUT_DAYS", "2"))
        history_days = int(os.getenv("V2_BACKTEST_DAYS", "30"))
        end = now.floor("15min") - pd.Timedelta(days=holdout_days)
        start = end - pd.Timedelta(days=history_days)
        universe_limit = int(os.getenv("V2_BACKTEST_UNIVERSE", "20"))
        min_turnover = float(os.getenv("V2_BACKTEST_MIN_TURNOVER_USDT", "10000000"))
        universe = self.provider.liquid_spot_usdt_symbols(
            limit=universe_limit,
            min_turnover=min_turnover,
        )
        dataset_id = _dataset_id(start, end, universe)
        state = {
            "dataset_id": dataset_id,
            "start": str(start),
            "end": str(end),
            "cursor": 0,
            "universe": universe,
            "complete": len(universe) == 0,
            "created_at": str(now),
            "survivorship_warning": True,
            "universe_method": "current_liquid_spot_universe",
            "spot_is_execution_truth": True,
            "perp_aux_features_included": False,
            "microstructure_history_included": False,
        }
        self.store.set_runtime(self.STATE_KEY, state)
        return state

    def state(self):
        row = self.store.get_runtime(self.STATE_KEY)
        return None if row is None else row.get("value")

    def ensure_state(self):
        state = self.state()
        if not state:
            return self._new_state()
        current_fp = _strategy_fingerprint()
        current_cfg = _strategy_config_snapshot()
        if state.get("strategy_fingerprint") != current_fp or state.get("strategy_config") != current_cfg:
            old_id = str(state.get("dataset_id") or "unknown")
            self.store.set_runtime(
                f"v2_backtest_superseded:{old_id}",
                {
                    **state,
                    "superseded_at": str(pd.Timestamp.now(tz="UTC")),
                    "superseded_reason": "strategy_or_config_changed",
                },
            )
            return self._new_state()
        return state

    def _benchmarks(self, state):
        key = (state["start"], state["end"])
        if key == self._bench_key and self._btc is not None and self._eth is not None:
            return self._btc, self._eth
        start = _to_utc(state["start"]) - pd.Timedelta(days=2)
        end = _to_utc(state["end"])
        self._btc = self.provider.kline_range("BTCUSDT", "15", _ms(start), _ms(end), category="spot")
        self._eth = self.provider.kline_range("ETHUSDT", "15", _ms(start), _ms(end), category="spot")
        self._bench_key = key
        return self._btc, self._eth

    def _symbol_key(self, dataset_id, symbol):
        return f"v2_backtest_symbol:{dataset_id}:{symbol}"

    def _process_symbol(self, state, symbol):
        start = _to_utc(state["start"])
        end = _to_utc(state["end"])
        warmup = start - pd.Timedelta(days=2)
        btc, eth = self._benchmarks(state)

        bars = self.provider.kline_range(symbol, "15", _ms(warmup), _ms(end), category="spot")
        if bars.empty or len(bars) < 80:
            result = {"symbol": symbol, "status": "empty", "trades": [], "signals": 0}
            self.store.set_runtime(self._symbol_key(state["dataset_id"], symbol), result)
            return result

        min_score = float(os.getenv("V2_BACKTEST_MIN_SCORE", "60"))
        cooldown_minutes = int(os.getenv("V2_SETUP_COOLDOWN_MINUTES", "60"))
        max_hold = int(os.getenv("V2_SHADOW_MAX_HOLD_MINUTES", "720"))
        fee_rate = float(os.getenv("V2_FEE_RATE", "0.001"))
        entry_slip = float(os.getenv("V2_BACKTEST_ENTRY_SLIPPAGE_PCT", "0.08"))
        exit_slip = float(os.getenv("V2_EXIT_SLIPPAGE_PCT", "0.05"))
        notional = float(os.getenv("V2_MAX_NOTIONAL_USDT", "5"))

        trades = []
        signals = 0
        last_setup_signal = {}

        valid_positions = [
            i for i, ts in enumerate(bars.index)
            if ts >= start and ts < end and i >= 60
        ]

        for i in valid_positions:
            ts = bars.index[i]
            window = bars.iloc[max(0, i-119):i+1]
            b = btc[btc.index <= ts].tail(120)
            e = eth[eth.index <= ts].tail(120)
            if len(b) < 60 or len(e) < 60:
                continue

            try:
                regime = detect_regime(b, e)
                f = compute_features(symbol, window, b, e, 0.0)
                setups = [x for x in evaluate_setups(f, regime) if x.score >= min_score]
            except Exception:
                continue
            if not setups:
                continue

            setups.sort(key=lambda x: x.score, reverse=True)
            candidate = setups[0]
            setup_key = candidate.setup
            last = last_setup_signal.get(setup_key)
            decision_time = ts + pd.Timedelta(minutes=15)
            if last is not None and (decision_time - last).total_seconds() < cooldown_minutes * 60:
                continue
            last_setup_signal[setup_key] = decision_time
            signals += 1

            path_end = min(
                decision_time + pd.Timedelta(minutes=max_hold + 2),
                end + pd.Timedelta(minutes=2),
            )
            try:
                bars1 = self.provider.kline_range(
                    symbol,
                    "1",
                    _ms(decision_time - pd.Timedelta(minutes=1)),
                    _ms(path_end),
                    category="spot",
                )
                simulated = simulate_long_path(
                    symbol=symbol,
                    bars1m=bars1,
                    entry_time=decision_time,
                    entry_price=float(candidate.signal_price),
                    notional_usdt=notional,
                    stop_pct=float(candidate.stop_pct),
                    target_pct=float(candidate.target_pct),
                    fee_rate=fee_rate,
                    entry_slippage_pct=entry_slip,
                    exit_slippage_pct=exit_slip,
                    max_hold_minutes=max_hold,
                ).to_dict()
            except Exception:
                continue

            simulated.update({
                "setup": candidate.setup,
                "score": float(candidate.score),
                "regime": candidate.regime,
                "stop_pct": float(candidate.stop_pct),
                "target_pct": float(candidate.target_pct),
                "features": candidate.features,
                "perp_features": None,
            })
            trades.append(simulated)

        result = {
            "symbol": symbol,
            "status": "ok",
            "signals": signals,
            "trades": trades,
            "bars15": len(bars),
        }
        self.store.set_runtime(self._symbol_key(state["dataset_id"], symbol), result)
        return {
            "symbol": symbol,
            "status": "ok",
            "signals": signals,
            "trades": len(trades),
            "bars15": len(bars),
        }

    def _all_trades(self, state):
        trades = []
        for symbol in state.get("universe") or []:
            row = self.store.get_runtime(self._symbol_key(state["dataset_id"], symbol))
            if row is None or not isinstance(row.get("value"), dict):
                continue
            trades.extend(row["value"].get("trades") or [])
        return trades

    def aggregate(self, state=None):
        state = state or self.ensure_state()
        raw = self._all_trades(state)
        start_eq = float(os.getenv("V2_SHADOW_START_EQUITY_USDT", "15"))
        max_trades = int(os.getenv("V2_MAX_TRADES_PER_DAY", "2"))
        daily_stop = float(os.getenv("V2_DAILY_STOP_USDT", "0.50"))

        sensitivity = {}
        for threshold in (60, 65, 70, 75):
            filtered = [t for t in raw if float(t.get("score") or 0.0) >= threshold]
            portfolio = _portfolio_filter(
                filtered,
                starting_equity=start_eq,
                max_trades_per_day=max_trades,
                daily_stop_usdt=daily_stop,
            )
            m = trade_metrics(portfolio, starting_equity=start_eq)
            sensitivity[str(threshold)] = {
                **{k: v for k, v in m.items() if k != "equity_curve"},
                "raw_signal_trades": len(filtered),
            }

        primary_threshold = int(os.getenv("V2_SHADOW_MIN_SCORE", "70"))
        primary_raw = [t for t in raw if float(t.get("score") or 0.0) >= primary_threshold]
        primary = _portfolio_filter(
            primary_raw,
            starting_equity=start_eq,
            max_trades_per_day=max_trades,
            daily_stop_usdt=daily_stop,
        )
        metrics = trade_metrics(primary, starting_equity=start_eq)
        mc = monte_carlo(primary, starting_equity=start_eq, simulations=2000) if len(primary) >= 5 else None

        return {
            "dataset_id": state["dataset_id"],
            "complete": bool(state.get("complete")),
            "cursor": int(state.get("cursor") or 0),
            "universe_size": len(state.get("universe") or []),
            "start": state["start"],
            "end": state["end"],
            "survivorship_warning": bool(state.get("survivorship_warning")),
            "perp_aux_features_included": False,
            "microstructure_history_included": False,
            "primary_score_threshold": primary_threshold,
            "raw_trade_count": len(raw),
            "portfolio_trade_count": len(primary),
            "metrics": metrics,
            "score_sensitivity": sensitivity,
            "walk_forward_7d": _fold_metrics(primary, starting_equity=start_eq, fold_days=7),
            "by_setup": grouped_trade_metrics(primary, "setup", starting_equity=start_eq),
            "by_regime": grouped_trade_metrics(primary, "regime", starting_equity=start_eq),
            "by_setup_regime": grouped_trade_metrics(
                [{**t, "setup_regime": f"{t.get('setup','UNKNOWN')}|{t.get('regime','UNKNOWN')}"} for t in primary],
                "setup_regime",
                starting_equity=start_eq,
            ),
            "monte_carlo": mc,
        }

    def run_batch(self, batch_size=1):
        state = self.ensure_state()
        universe = state.get("universe") or []
        cursor = int(state.get("cursor") or 0)
        processed = []

        for _ in range(max(1, int(batch_size))):
            if cursor >= len(universe):
                break
            symbol = universe[cursor]
            try:
                processed.append(self._process_symbol(state, symbol))
            except Exception as exc:
                processed.append({"symbol": symbol, "status": "error", "error": repr(exc)[:300]})
            cursor += 1
            state["cursor"] = cursor
            state["complete"] = cursor >= len(universe)
            state["updated_at"] = str(pd.Timestamp.now(tz="UTC"))
            self.store.set_runtime(self.STATE_KEY, state)

        aggregate = self.aggregate(state)
        self.store.set_runtime("v2_backtest_summary", aggregate)
        return {
            "dataset_id": state["dataset_id"],
            "cursor": cursor,
            "universe_size": len(universe),
            "complete": bool(state.get("complete")),
            "processed": processed,
            "summary": {
                "portfolio_trade_count": aggregate.get("portfolio_trade_count"),
                "ending_equity_usdt": (aggregate.get("metrics") or {}).get("ending_equity_usdt"),
                "expectancy_usdt": (aggregate.get("metrics") or {}).get("expectancy_usdt"),
                "max_drawdown_pct": (aggregate.get("metrics") or {}).get("max_drawdown_pct"),
            },
        }
