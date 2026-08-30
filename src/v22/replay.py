from __future__ import annotations

import os
import pandas as pd

from src.v2.provider import BybitV2Provider
from src.v2.research import trade_metrics, grouped_trade_metrics
from .runner import evaluate_runner_path


def _utc(ts):
    x = pd.Timestamp(ts)
    return x.tz_localize("UTC") if x.tzinfo is None else x


def _ms(ts):
    return int(_utc(ts).timestamp() * 1000)


class V22RunnerReplay:
    STATE_KEY = "v22_runner_replay_state"

    def __init__(self, store, provider=None):
        self.store = store
        self.provider = provider or BybitV2Provider()

    def _source_state(self):
        row = self.store.get_runtime("v2_backtest_state")
        return None if row is None else row.get("value")

    def _state(self):
        source = self._source_state()
        if not isinstance(source, dict):
            return None
        dataset_id = source.get("dataset_id")
        row = self.store.get_runtime(self.STATE_KEY)
        state = None if row is None else row.get("value")
        if not isinstance(state, dict) or state.get("source_dataset_id") != dataset_id:
            state = {
                "source_dataset_id": dataset_id,
                "cursor": 0,
                "universe": list(source.get("universe") or []),
                "complete": False,
                "created_at": str(pd.Timestamp.now(tz="UTC")),
                "runner_version": "2.2",
            }
            self.store.set_runtime(self.STATE_KEY, state)
        return state

    def _source_symbol(self, dataset_id, symbol):
        row = self.store.get_runtime(f"v2_backtest_symbol:{dataset_id}:{symbol}")
        return None if row is None else row.get("value")

    def _result_key(self, dataset_id, symbol):
        return f"v22_runner_replay:{dataset_id}:{symbol}"

    def _replay_trade(self, trade):
        entry_time = _utc(trade["entry_time"])
        max_hold = int(os.getenv("V22_MAX_HOLD_MINUTES", "1440"))
        end = entry_time + pd.Timedelta(minutes=max_hold + 2)
        bars = self.provider.kline_range(
            trade["symbol"], "1",
            _ms(entry_time - pd.Timedelta(minutes=1)),
            _ms(end),
            category="spot",
        )
        result = evaluate_runner_path(
            bars1m=bars,
            entry_time=entry_time,
            entry_price=float(trade["entry_price"]),
            notional_usdt=float(trade.get("notional_usdt") or 5.0),
            initial_stop_pct=float(trade.get("stop_pct") or 1.0),
            fee_rate=float(os.getenv("V2_FEE_RATE", "0.001")),
            entry_slippage_pct=0.0,
            exit_slippage_pct=float(os.getenv("V2_EXIT_SLIPPAGE_PCT", "0.05")),
            partial_r=float(os.getenv("V22_PARTIAL_R", "1.5")),
            partial_fraction=float(os.getenv("V22_PARTIAL_FRACTION", "0.5")),
            trail_pct=float(os.getenv("V22_TRAIL_PCT", "1.2")),
            breakeven_buffer_pct=float(os.getenv("V22_BREAKEVEN_BUFFER_PCT", "0.30")),
            max_hold_minutes=max_hold,
        )
        out = {
            **trade,
            "source_fixed_pnl_usdt": float(trade.get("pnl_usdt") or 0.0),
            "runner_pnl_usdt": float(result.total_pnl_usdt),
            "pnl_usdt": float(result.total_pnl_usdt),
            "runner_status": result.status,
            "runner_partial_hit": result.partial_hit,
            "runner_exit_reason": result.final_exit_reason,
            "runner_exit_time": result.final_exit_time,
            "runner_exit_price": result.final_exit_price,
            "runner_highest_price": result.highest_price,
            "runner_active_stop_price": result.active_stop_price,
            "runner_delta_vs_fixed_usdt": float(result.total_pnl_usdt) - float(trade.get("pnl_usdt") or 0.0),
            "runner_version": "2.2",
        }
        return out

    def _process_symbol(self, state, symbol):
        source = self._source_symbol(state["source_dataset_id"], symbol)
        if not isinstance(source, dict):
            return {"symbol": symbol, "status": "source_not_ready", "trades": 0}

        source_trades = source.get("trades") or []
        out = []
        errors = []
        for trade in source_trades:
            try:
                out.append(self._replay_trade(trade))
            except Exception as exc:
                errors.append(repr(exc)[:180])

        result = {
            "symbol": symbol,
            "status": "ok",
            "source_trades": len(source_trades),
            "runner_trades": out,
            "errors": errors[:10],
        }
        self.store.set_runtime(self._result_key(state["source_dataset_id"], symbol), result)
        return {
            "symbol": symbol,
            "status": "ok",
            "source_trades": len(source_trades),
            "runner_trades": len(out),
            "errors": len(errors),
        }

    def aggregate(self, state=None):
        state = state or self._state()
        if not state:
            return {"status": "source_backtest_not_ready"}

        fixed = []
        runner = []
        deltas = []
        for symbol in state.get("universe") or []:
            row = self.store.get_runtime(self._result_key(state["source_dataset_id"], symbol))
            value = None if row is None else row.get("value")
            if not isinstance(value, dict):
                continue
            for t in value.get("runner_trades") or []:
                runner.append(t)
                fixed.append({**t, "pnl_usdt": float(t.get("source_fixed_pnl_usdt") or 0.0)})
                deltas.append(float(t.get("runner_delta_vs_fixed_usdt") or 0.0))

        start = float(os.getenv("V22_SHADOW_START_EQUITY_USDT", "15"))
        fixed_m = trade_metrics(fixed, starting_equity=start)
        runner_m = trade_metrics(runner, starting_equity=start)

        return {
            "source_dataset_id": state["source_dataset_id"],
            "complete": bool(state.get("complete")),
            "cursor": int(state.get("cursor") or 0),
            "universe_size": len(state.get("universe") or []),
            "matched_trades": len(runner),
            "fixed_metrics": fixed_m,
            "runner_metrics": runner_m,
            "runner_minus_fixed_total_usdt": round(sum(deltas), 8),
            "runner_better_trade_fraction": (
                None if not deltas else sum(x > 0 for x in deltas) / len(deltas)
            ),
            "runner_by_setup": grouped_trade_metrics(runner, "setup", starting_equity=start),
            "runner_by_regime": grouped_trade_metrics(runner, "regime", starting_equity=start),
        }

    def run_batch(self, batch_size=1):
        state = self._state()
        if not state:
            return {"status": "source_backtest_not_ready"}

        universe = state.get("universe") or []
        cursor = int(state.get("cursor") or 0)
        processed = []
        for _ in range(max(1, int(batch_size))):
            if cursor >= len(universe):
                break
            symbol = universe[cursor]
            processed.append(self._process_symbol(state, symbol))
            cursor += 1
            state["cursor"] = cursor
            state["complete"] = cursor >= len(universe)
            state["updated_at"] = str(pd.Timestamp.now(tz="UTC"))
            self.store.set_runtime(self.STATE_KEY, state)

        summary = self.aggregate(state)
        self.store.set_runtime("v22_runner_replay_summary", summary)
        return {
            "cursor": cursor,
            "universe_size": len(universe),
            "complete": bool(state.get("complete")),
            "processed": processed,
            "summary": {
                "matched_trades": summary.get("matched_trades"),
                "runner_minus_fixed_total_usdt": summary.get("runner_minus_fixed_total_usdt"),
                "runner_better_trade_fraction": summary.get("runner_better_trade_fraction"),
            },
        }
