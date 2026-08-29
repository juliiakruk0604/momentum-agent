from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
import pandas as pd

from .continuation import evaluate_continuation
from .impulse import compute_impulse_candidates
from .labeling import label_future_moves
from .models import DerivativesSnapshot


def _ms(ts):
    return int(pd.Timestamp(ts).timestamp() * 1000)


def _config_fingerprint(cfg):
    payload = json.dumps(cfg, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def active_overlap(meta, start, end):
    launch_raw = int(meta.get("launchTime") or 0)
    launch = pd.to_datetime(launch_raw, unit="ms", utc=True)
    delivery_raw = int(meta.get("deliveryTime") or 0)
    delivery = None if delivery_raw <= 0 else pd.to_datetime(delivery_raw, unit="ms", utc=True)
    return launch <= end and (delivery is None or delivery > start)


def build_folds(start, end, cfg):
    wf = cfg["walk_forward"]
    train = pd.Timedelta(days=int(wf["train_days"]))
    test = pd.Timedelta(days=int(wf["test_days"]))
    step = pd.Timedelta(days=int(wf["step_days"]))
    folds = []
    test_start = start + train
    fold_id = 0
    while test_start < end:
        test_end = min(test_start + test, end)
        folds.append({
            "fold_id": fold_id,
            "train_start": test_start - train,
            "train_end": test_start,
            "test_start": test_start,
            "test_end": test_end,
        })
        fold_id += 1
        test_start += step
    return folds


def fold_for(ts, folds):
    ts = pd.Timestamp(ts)
    for fold in folds:
        if fold["test_start"] <= ts < fold["test_end"]:
            return int(fold["fold_id"])
    return None


def historical_derivatives(oi, funding, available_time, publication_lag_bars=1):
    ts = pd.Timestamp(available_time)
    oi_change = None
    oi_hist = oi[oi.index <= ts].copy() if oi is not None and len(oi) else None
    if oi_hist is not None and len(oi_hist):
        lag = max(0, int(publication_lag_bars))
        usable = oi_hist.iloc[:-lag] if lag and len(oi_hist) > lag else (oi_hist.iloc[0:0] if lag else oi_hist)
        if len(usable) >= 5:
            a = float(usable.iloc[-5]["open_interest"])
            b = float(usable.iloc[-1]["open_interest"])
            if a > 0:
                oi_change = (b / a - 1.0) * 100.0

    funding_rate = None
    f_hist = funding[funding.index <= ts] if funding is not None and len(funding) else None
    if f_hist is not None and len(f_hist):
        funding_rate = float(f_hist.iloc[-1]["funding_rate"])

    return DerivativesSnapshot(
        oi_change_1h_pct=oi_change,
        funding_rate=funding_rate,
        source="BYBIT_HISTORICAL",
    )


class HistoricalBackfillRunner:
    """Incremental point-in-time OOS dataset builder."""

    STATE_KEY = "historical_backfill_state"

    def __init__(self, provider, store, cfg):
        self.provider = provider
        self.store = store
        self.cfg = cfg
        self._benchmark_key = None
        self._btc = None
        self._eth = None

    def _new_state(self, now=None):
        now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
        if now.tzinfo is None:
            now = now.tz_localize("UTC")
        holdout_days = int(os.getenv("HISTORICAL_HOLDOUT_DAYS", "10"))
        history_days = int(os.getenv("HISTORICAL_BACKFILL_DAYS", "60"))
        end = now.floor("15min") - pd.Timedelta(days=holdout_days)
        start = end - pd.Timedelta(days=history_days)

        raw = self.provider.instruments_all_statuses(self.cfg["research"]["statuses"])
        universe = [
            m for m in raw
            if m.get("quoteCoin") == "USDT"
            and m.get("contractType") == "LinearPerpetual"
            and active_overlap(m, start, end)
        ]
        universe = sorted(universe, key=lambda m: m.get("symbol", ""))
        closed_or_delivered = sum(
            1 for m in universe
            if str(m.get("status")) == "Closed" or int(m.get("deliveryTime") or 0) > 0
        )
        fingerprint = _config_fingerprint(self.cfg)
        dataset_id = f"bybit_oos_{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}_v321_{fingerprint}"
        state = {
            "dataset_id": dataset_id,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "cursor": 0,
            "universe": [
                {
                    "symbol": m.get("symbol"),
                    "status": m.get("status"),
                    "launchTime": m.get("launchTime"),
                    "deliveryTime": m.get("deliveryTime"),
                }
                for m in universe
            ],
            "closed_or_delivered_contracts": closed_or_delivered,
            "survivorship_warning": closed_or_delivered == 0,
            "config_fingerprint": fingerprint,
            "config_mismatch": False,
            "complete": len(universe) == 0,
            "created_at": now.isoformat(),
        }
        self.store.set_runtime(self.STATE_KEY, state)
        return state

    def state(self):
        row = self.store.get_runtime(self.STATE_KEY)
        return None if row is None else row.get("value")

    def ensure_state(self, now=None):
        state = self.state()
        if not state:
            return self._new_state(now)

        fingerprint = _config_fingerprint(self.cfg)
        pinned = state.get("config_fingerprint")
        if not pinned:
            state["config_fingerprint"] = fingerprint
            state["config_fingerprint_pinned_at"] = pd.Timestamp.now(tz="UTC").isoformat()
            state["config_mismatch"] = False
            self.store.set_runtime(self.STATE_KEY, state)
            return state

        mismatch = pinned != fingerprint
        state["config_mismatch"] = mismatch
        if mismatch:
            state["observed_config_fingerprint"] = fingerprint
            state["complete"] = False
            state["updated_at"] = pd.Timestamp.now(tz="UTC").isoformat()
            self.store.set_runtime(self.STATE_KEY, state)
        else:
            state.pop("observed_config_fingerprint", None)
        return state

    def _benchmarks(self, state):
        key = (state["start"], state["end"])
        if key == self._benchmark_key and self._btc is not None and self._eth is not None:
            return self._btc, self._eth
        start = pd.Timestamp(state["start"])
        end = pd.Timestamp(state["end"])
        warmup = start - pd.Timedelta(days=2)
        self._btc = self.provider.kline_range("BTCUSDT", "15", _ms(warmup), _ms(end))
        self._eth = self.provider.kline_range("ETHUSDT", "15", _ms(warmup), _ms(end))
        self._benchmark_key = key
        return self._btc, self._eth

    def _process_symbol(self, state, meta):
        symbol = meta["symbol"]
        start = pd.Timestamp(state["start"])
        end = pd.Timestamp(state["end"])
        warmup = start - pd.Timedelta(days=2)
        label_end = end + pd.Timedelta(minutes=max(self.cfg["labeling"]["horizons_minutes"]) + 30)
        btc, eth = self._benchmarks(state)

        bars15 = self.provider.kline_range(symbol, "15", _ms(warmup), _ms(label_end))
        if bars15.empty:
            self.store.record_historical_symbol_run(
                state["dataset_id"], symbol, "empty", 0, 0, 0, 0, None
            )
            return {"symbol": symbol, "status": "empty", "impulses": 0}

        impulses = compute_impulse_candidates(symbol, bars15, btc, eth, self.cfg)
        impulses = [imp for imp in impulses if start <= imp.signal_time < end]

        oi = self.provider.open_interest_range(symbol, _ms(warmup), _ms(end), "15min")
        funding = self.provider.funding_history_range(symbol, _ms(warmup), _ms(end))
        folds = build_folds(start, end, self.cfg)
        stored = 0
        exact5_errors = 0

        for imp in impulses:
            try:
                cont_end = imp.available_time + pd.Timedelta(
                    minutes=int(self.cfg["continuation"]["observation_minutes"]) + 10
                )
                bars5 = self.provider.kline_range(symbol, "5", _ms(imp.available_time), _ms(cont_end))
                cont = evaluate_continuation(imp, bars5, self.cfg)
            except Exception:
                exact5_errors += 1
                continue

            deriv = historical_derivatives(
                oi,
                funding,
                imp.available_time,
                publication_lag_bars=int(self.cfg["research"].get("publication_lag_bars", 1)),
            )
            labels = label_future_moves(imp, bars15, self.cfg)
            fold_id = fold_for(imp.available_time, folds)
            self.store.upsert_historical_event(
                state["dataset_id"], imp, cont, deriv, [asdict(x) for x in labels], fold_id
            )
            stored += 1

        run_status = "ok" if exact5_errors == 0 else "partial"
        self.store.record_historical_symbol_run(
            state["dataset_id"],
            symbol,
            run_status,
            int(len(bars15)),
            int(stored),
            int(len(oi)),
            int(len(funding)),
            None if exact5_errors == 0 else f"exact5_errors={exact5_errors}",
        )
        return {
            "symbol": symbol,
            "status": run_status,
            "bars15": int(len(bars15)),
            "impulses": int(stored),
            "oi_rows": int(len(oi)),
            "funding_rows": int(len(funding)),
            "exact5_errors": exact5_errors,
        }

    def _problem_runs(self, dataset_id):
        return self.store._execute(
            '''SELECT symbol,status,error FROM historical_symbol_runs
               WHERE dataset_id=? AND (
                 status IN ('empty','partial','error')
                 OR (status='ok' AND error LIKE 'exact5_errors=%')
               ) ORDER BY symbol''',
            '''SELECT symbol,status,error FROM historical_symbol_runs
               WHERE dataset_id=%s AND (
                 status IN ('empty','partial','error')
                 OR (status='ok' AND error LIKE 'exact5_errors=%%')
               ) ORDER BY symbol''',
            (dataset_id,),
            fetch="all",
        )

    def _clear_symbol_events(self, dataset_id, symbol):
        self.store._execute(
            "DELETE FROM historical_events WHERE dataset_id=? AND symbol=?",
            "DELETE FROM historical_events WHERE dataset_id=%s AND symbol=%s",
            (dataset_id, symbol),
        )

    @staticmethod
    def _meta_for_symbol(universe, symbol):
        for meta in universe:
            if meta.get("symbol") == symbol:
                return meta
        return None

    def _run_symbol(self, state, meta):
        symbol = meta["symbol"]
        try:
            return self._process_symbol(state, meta)
        except Exception as exc:
            self.store.record_historical_symbol_run(
                state["dataset_id"], symbol, "error", 0, 0, 0, 0, repr(exc)
            )
            return {"symbol": symbol, "status": "error", "error": repr(exc)}

    def run_batch(self, batch_size=1):
        state = self.ensure_state()
        universe = state.get("universe") or []
        cursor = int(state.get("cursor") or 0)
        batch_size = max(1, int(batch_size))

        if state.get("config_mismatch"):
            return {
                "dataset_id": state["dataset_id"],
                "complete": False,
                "primary_complete": cursor >= len(universe),
                "cursor": cursor,
                "universe_size": len(universe),
                "processed": [],
                "config_mismatch": True,
                "config_fingerprint": state.get("config_fingerprint"),
                "observed_config_fingerprint": state.get("observed_config_fingerprint"),
            }

        if cursor < len(universe):
            processed = []
            for _ in range(batch_size):
                if cursor >= len(universe):
                    break
                meta = universe[cursor]
                processed.append(self._run_symbol(state, meta))
                cursor += 1
                state["cursor"] = cursor
                state["primary_complete"] = cursor >= len(universe)
                state["complete"] = False
                state["updated_at"] = pd.Timestamp.now(tz="UTC").isoformat()
                self.store.set_runtime(self.STATE_KEY, state)

            return {
                "dataset_id": state["dataset_id"],
                "complete": False,
                "primary_complete": cursor >= len(universe),
                "cursor": cursor,
                "universe_size": len(universe),
                "processed": processed,
            }

        state["primary_complete"] = True
        retry_attempts = dict(state.get("retry_attempts") or {})
        max_retry_attempts = max(1, int(os.getenv("HISTORICAL_BACKFILL_RETRY_ATTEMPTS", "2")))
        problem_rows = self._problem_runs(state["dataset_id"])
        retryable = [
            row for row in problem_rows
            if int(retry_attempts.get(row["symbol"], 0)) < max_retry_attempts
        ]
        processed = []

        for row in retryable[:batch_size]:
            symbol = row["symbol"]
            meta = self._meta_for_symbol(universe, symbol)
            retry_attempts[symbol] = int(retry_attempts.get(symbol, 0)) + 1
            if meta is None:
                processed.append({
                    "symbol": symbol,
                    "status": "error",
                    "retry": True,
                    "attempt": retry_attempts[symbol],
                    "error": "symbol_not_in_backfill_universe",
                })
                continue
            self._clear_symbol_events(state["dataset_id"], symbol)
            result = self._run_symbol(state, meta)
            processed.append({**result, "retry": True, "attempt": retry_attempts[symbol]})

        remaining_problem_rows = self._problem_runs(state["dataset_id"])
        retryable_remaining = [
            row for row in remaining_problem_rows
            if int(retry_attempts.get(row["symbol"], 0)) < max_retry_attempts
        ]
        unresolved = [row["symbol"] for row in remaining_problem_rows]
        state["retry_attempts"] = retry_attempts
        state["unresolved_retry_symbols"] = unresolved
        state["complete"] = len(retryable_remaining) == 0
        state["updated_at"] = pd.Timestamp.now(tz="UTC").isoformat()
        self.store.set_runtime(self.STATE_KEY, state)

        return {
            "dataset_id": state["dataset_id"],
            "complete": state["complete"],
            "primary_complete": True,
            "cursor": cursor,
            "universe_size": len(universe),
            "processed": processed,
            "retry_remaining": len(retryable_remaining),
            "unresolved_retry_symbols": unresolved,
        }
