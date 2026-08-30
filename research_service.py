from __future__ import annotations

import json
import os
import time

import pandas as pd

from src.historical_backfill import HistoricalBackfillRunner
from src.providers.bybit_public import BybitPublicProvider
from src.store import SignalStore
from src.v22.replay import V22RunnerReplay
from src.v22.labels import V22FlowLabeler
from src.v2.backtest import (
    V2BacktestRunner,
    _strategy_config_snapshot,
    _strategy_fingerprint,
)
from worker import load_cfg, _prepare_provenance_rebuild


def _ensure_v2_provenance(v2: V2BacktestRunner):
    current_fp = _strategy_fingerprint()
    current_cfg = _strategy_config_snapshot()
    state = v2.state()

    if (
        state
        and state.get("strategy_fingerprint") == current_fp
        and state.get("strategy_config") == current_cfg
    ):
        return state

    if state:
        old_id = str(state.get("dataset_id") or "unknown")
        v2.store.set_runtime(
            f"v2_backtest_superseded:{old_id}",
            {
                **state,
                "superseded_at": str(pd.Timestamp.now(tz="UTC")),
                "superseded_reason": "missing_or_changed_strategy_provenance",
            },
        )

    state = v2._new_state()
    state["strategy_fingerprint"] = current_fp
    state["strategy_config"] = current_cfg
    v2.store.set_runtime(v2.STATE_KEY, state)
    return state


def main():
    store = SignalStore()
    cfg = load_cfg()
    provider = BybitPublicProvider()

    v1_enabled = os.getenv("HISTORICAL_BACKFILL_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    v2_enabled = os.getenv("V2_BACKTEST_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    v22_enabled = os.getenv("V22_REPLAY_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    v22_labeler_enabled = os.getenv("V22_FLOW_LABELER_ENABLED", "true").lower() in ("1", "true", "yes", "on")

    v1 = HistoricalBackfillRunner(provider, store, cfg) if v1_enabled else None
    v2 = V2BacktestRunner(store) if v2_enabled else None
    v22 = V22RunnerReplay(store) if v22_enabled else None
    v22_labeler = V22FlowLabeler(store) if v22_labeler_enabled else None

    v1_batch = max(1, int(os.getenv("HISTORICAL_BACKFILL_SYMBOLS_PER_SCAN", "5")))
    v2_batch = max(1, int(os.getenv("V2_BACKTEST_SYMBOLS_PER_CYCLE", "1")))
    v22_batch = max(1, int(os.getenv("V22_REPLAY_SYMBOLS_PER_CYCLE", "1")))
    sleep_seconds = max(5, int(os.getenv("RESEARCH_SLEEP_SECONDS", "10")))

    print("RESEARCH_SERVICE_START", json.dumps({
        "v1_enabled": v1_enabled,
        "v2_enabled": v2_enabled,
        "v22_enabled": v22_enabled,
        "v22_labeler_enabled": v22_labeler_enabled,
        "v1_batch": v1_batch,
        "v2_batch": v2_batch,
        "v22_batch": v22_batch,
    }), flush=True)

    while True:
        cycle_started = pd.Timestamp.now(tz="UTC")
        cycle = {
            "started_at": str(cycle_started),
            "v1": None,
            "v2": None,
            "v22": None,
            "v22_labels": None,
            "errors": [],
        }

        if v2 is not None:
            try:
                state = _ensure_v2_provenance(v2)
                print("RESEARCH_V2_START", json.dumps({"cursor": state.get("cursor")}), flush=True)
                result = v2.run_batch(v2_batch)
                cycle["v2"] = result
                store.set_runtime("v2_backtest_last_batch", result)
                store.set_runtime("v2_backtest_error", None)
                print("RESEARCH_V2", json.dumps(result, default=str), flush=True)
            except Exception as exc:
                err = {"component": "v2", "error": repr(exc), "time": str(pd.Timestamp.now(tz="UTC"))}
                cycle["errors"].append(err)
                store.set_runtime("v2_backtest_error", err)
                print("RESEARCH_V2_ERROR", repr(exc), flush=True)

        if v22 is not None:
            try:
                print("RESEARCH_V22_START", flush=True)
                result = v22.run_batch(v22_batch)
                cycle["v22"] = result
                store.set_runtime("v22_runner_replay_last_batch", result)
                store.set_runtime("v22_runner_replay_error", None)
                print("RESEARCH_V22", json.dumps(result, default=str), flush=True)
            except Exception as exc:
                err = {"component": "v22", "error": repr(exc), "time": str(pd.Timestamp.now(tz="UTC"))}
                cycle["errors"].append(err)
                store.set_runtime("v22_runner_replay_error", err)
                print("RESEARCH_V22_ERROR", repr(exc), flush=True)

        if v22_labeler is not None:
            try:
                label_result = v22_labeler.run_batch()
                cycle["v22_labels"] = label_result
                store.set_runtime("v22_flow_labeler_error", None)
                print("RESEARCH_V22_LABELS", json.dumps(label_result, default=str), flush=True)
            except Exception as exc:
                err = {"component": "v22_labels", "error": repr(exc), "time": str(pd.Timestamp.now(tz="UTC"))}
                cycle["errors"].append(err)
                store.set_runtime("v22_flow_labeler_error", err)
                print("RESEARCH_V22_LABELS_ERROR", repr(exc), flush=True)

        if v1 is not None:
            try:
                print("RESEARCH_V1_START", json.dumps({"cursor": (v1.state() or {}).get("cursor")}), flush=True)
                rebuild = _prepare_provenance_rebuild(v1, store)
                if rebuild is not None:
                    store.set_runtime("historical_backfill_rebuild", rebuild)
                    print("RESEARCH_V1_REBUILD", json.dumps(rebuild, default=str), flush=True)
                result = v1.run_batch(v1_batch)
                cycle["v1"] = result
                store.set_runtime("historical_backfill_last_batch", result)
                store.set_runtime("historical_backfill_error", None)
                print("RESEARCH_V1", json.dumps(result, default=str), flush=True)
            except Exception as exc:
                err = {"component": "v1", "error": repr(exc), "time": str(pd.Timestamp.now(tz="UTC"))}
                cycle["errors"].append(err)
                store.set_runtime("historical_backfill_error", err)
                print("RESEARCH_V1_ERROR", repr(exc), flush=True)

        cycle["finished_at"] = str(pd.Timestamp.now(tz="UTC"))
        store.set_runtime("research_worker_heartbeat", cycle)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
