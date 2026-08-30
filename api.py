from __future__ import annotations

import os
from fastapi import FastAPI, HTTPException

from src.store import SignalStore
from src.execution.preflight import build_execution_plan
from src.execution.exchange_constraints import bybit_linear_constraints
from src.promo_scanner import scan_promos
from src.bybit_account import funding_balances
from src.execution.bybit_spot import dry_run_suite
from src.shadow_portfolio import summary as shadow_summary
from src.v2.shadow import summary as v2_shadow_summary
from src.v2.readiness import evaluate_v2_readiness
from src.v22.shadow import summary as v22_shadow_summary
from src.v22.readiness import evaluate_v22_readiness

app = FastAPI(title="Momentum Research Agent", version="3.3.6")
store = SignalStore()


def _historical_backfill_errors(historical):
    progress = (historical or {}).get("progress") or {}
    quality = progress.get("quality") or []
    return sum(
        int(row.get("n") or 0)
        for row in quality
        if str(row.get("status") or "").lower() == "error"
    )


def _historical_backfill_partial_runs(historical):
    progress = (historical or {}).get("progress") or {}
    quality = progress.get("quality") or []
    return sum(
        int(row.get("n") or 0)
        for row in quality
        if str(row.get("status") or "").lower() == "partial"
    )


def _historical_backfill_empty_runs(historical):
    progress = (historical or {}).get("progress") or {}
    quality = progress.get("quality") or []
    return sum(
        int(row.get("n") or 0)
        for row in quality
        if str(row.get("status") or "").lower() == "empty"
    )


def _historical_backfill_empty_symbols(historical):
    dataset_id = (historical or {}).get("dataset_id")
    if not dataset_id or not hasattr(store, "_execute"):
        return []
    rows = store._execute(
        '''SELECT symbol FROM historical_symbol_runs
           WHERE dataset_id=? AND status='empty' ORDER BY symbol''',
        '''SELECT symbol FROM historical_symbol_runs
           WHERE dataset_id=%s AND status='empty' ORDER BY symbol''',
        (dataset_id,),
        fetch="all",
    ) or []
    return [str(row.get("symbol")) for row in rows if row.get("symbol")]


def _historical_backfill_unavailable_symbols(historical):
    empty_symbols = set(_historical_backfill_empty_symbols(historical))
    if not empty_symbols or not hasattr(store, "get_runtime"):
        return []
    runtime = store.get_runtime("historical_backfill_state") or {}
    state = runtime.get("value") or {}
    universe = state.get("universe") or []
    unavailable = []
    for meta in universe:
        symbol = str(meta.get("symbol") or "")
        if symbol not in empty_symbols:
            continue
        status = str(meta.get("status") or "")
        delivery_raw = int(meta.get("deliveryTime") or 0)
        if status == "Closed" or delivery_raw > 0:
            unavailable.append(symbol)
    return sorted(set(unavailable))


def _historical_backfill_legacy_partial_runs(historical):
    dataset_id = (historical or {}).get("dataset_id")
    if not dataset_id or not hasattr(store, "_execute"):
        return 0
    row = store._execute(
        '''SELECT COUNT(*) AS n FROM historical_symbol_runs
           WHERE dataset_id=? AND status='ok' AND error LIKE 'exact5_errors=%' ''',
        '''SELECT COUNT(*) AS n FROM historical_symbol_runs
           WHERE dataset_id=%s AND status='ok' AND error LIKE 'exact5_errors=%%' ''',
        (dataset_id,),
        fetch="one",
    )
    return int((row or {}).get("n") or 0)


def _historical_status_with_integrity_gate(historical=None):
    historical = historical or store.historical_status()
    oos = historical.get("oos")
    if not oos:
        return historical

    base_gate = oos.get("research_gate") or {}
    reasons = list(base_gate.get("reasons") or [])
    if historical.get("status") != "complete" and "historical_backfill_not_complete" not in reasons:
        reasons.append("historical_backfill_not_complete")

    backfill_errors = _historical_backfill_errors(historical)
    partial_runs = _historical_backfill_partial_runs(historical)
    legacy_partial_runs = _historical_backfill_legacy_partial_runs(historical)
    empty_runs = _historical_backfill_empty_runs(historical)
    unavailable_symbols = _historical_backfill_unavailable_symbols(historical)
    unavailable_count = len(unavailable_symbols)
    transient_empty_count = max(0, empty_runs - unavailable_count)

    if backfill_errors > 0 and "historical_backfill_has_errors" not in reasons:
        reasons.append("historical_backfill_has_errors")
    if partial_runs + legacy_partial_runs > 0 and "historical_backfill_has_partial_runs" not in reasons:
        reasons.append("historical_backfill_has_partial_runs")
    if unavailable_count > 0 and "historical_backfill_has_unavailable_market_data" not in reasons:
        reasons.append("historical_backfill_has_unavailable_market_data")
    if transient_empty_count > 0 and "historical_backfill_has_empty_runs" not in reasons:
        reasons.append("historical_backfill_has_empty_runs")

    return {
        **historical,
        "oos": {
            **oos,
            "research_gate": {
                **base_gate,
                "passed": len(reasons) == 0,
                "reasons": reasons,
            },
        },
        "integrity": {
            "backfill_error_count": backfill_errors,
            "backfill_partial_run_count": partial_runs + legacy_partial_runs,
            "backfill_legacy_partial_run_count": legacy_partial_runs,
            "backfill_empty_run_count": empty_runs,
            "backfill_transient_empty_count": transient_empty_count,
            "backfill_unavailable_market_data_count": unavailable_count,
            "backfill_unavailable_market_data_symbols": unavailable_symbols,
        },
    }


def _micro_live_readiness():
    readiness = store.micro_live_readiness()
    historical = readiness.get("historical") or store.historical_status()
    historical = _historical_status_with_integrity_gate(historical)
    reasons = list(readiness.get("reasons") or [])

    if historical.get("status") != "complete":
        if "historical_backfill_not_complete" not in reasons:
            reasons.append("historical_backfill_not_complete")

    backfill_errors = _historical_backfill_errors(historical)
    if backfill_errors > 0 and "historical_backfill_has_errors" not in reasons:
        reasons.append("historical_backfill_has_errors")

    partial_runs = _historical_backfill_partial_runs(historical)
    legacy_partial_runs = _historical_backfill_legacy_partial_runs(historical)
    total_partial_runs = partial_runs + legacy_partial_runs
    if total_partial_runs > 0 and "historical_backfill_has_partial_runs" not in reasons:
        reasons.append("historical_backfill_has_partial_runs")

    integrity = historical.get("integrity") or {}
    empty_runs = int(integrity.get("backfill_empty_run_count") or _historical_backfill_empty_runs(historical))
    unavailable_count = int(integrity.get("backfill_unavailable_market_data_count") or 0)
    transient_empty_count = int(integrity.get("backfill_transient_empty_count") or max(0, empty_runs - unavailable_count))
    unavailable_symbols = list(integrity.get("backfill_unavailable_market_data_symbols") or [])

    if unavailable_count > 0 and "historical_backfill_has_unavailable_market_data" not in reasons:
        reasons.append("historical_backfill_has_unavailable_market_data")
    if transient_empty_count > 0 and "historical_backfill_has_empty_runs" not in reasons:
        reasons.append("historical_backfill_has_empty_runs")

    historical_research_gate = ((historical.get("oos") or {}).get("research_gate") or {})
    if historical.get("oos") and not historical_research_gate.get("passed", False):
        if "historical_research_gate_not_passed" not in reasons:
            reasons.append("historical_research_gate_not_passed")

    if reasons != list(readiness.get("reasons") or []) or historical is not readiness.get("historical"):
        readiness = {
            **readiness,
            "ready": False if reasons else bool(readiness.get("ready")),
            "reasons": reasons,
            "historical": historical,
            "historical_backfill_error_count": backfill_errors,
            "historical_backfill_partial_run_count": total_partial_runs,
            "historical_backfill_legacy_partial_run_count": legacy_partial_runs,
            "historical_backfill_empty_run_count": empty_runs,
            "historical_backfill_transient_empty_count": transient_empty_count,
            "historical_backfill_unavailable_market_data_count": unavailable_count,
            "historical_backfill_unavailable_market_data_symbols": unavailable_symbols,
        }
    else:
        readiness = {
            **readiness,
            "historical_backfill_error_count": backfill_errors,
            "historical_backfill_partial_run_count": total_partial_runs,
            "historical_backfill_legacy_partial_run_count": legacy_partial_runs,
            "historical_backfill_empty_run_count": empty_runs,
            "historical_backfill_transient_empty_count": transient_empty_count,
            "historical_backfill_unavailable_market_data_count": unavailable_count,
            "historical_backfill_unavailable_market_data_symbols": unavailable_symbols,
        }
    return readiness


@app.get("/health")
def health():
    try:
        db_ok = store.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database_unhealthy: {exc}")
    worker = store.worker_health(int(os.getenv("WORKER_STALE_SECONDS", "300")))
    overall = "ok" if db_ok and worker["status"] in ("healthy", "starting") else "degraded"
    return {
        "status": overall,
        "mode": os.getenv("MODE", "shadow"),
        "live_execution": False,
        "database": store.backend,
        "worker_health": worker,
        "pipeline": [
            "EARLY_IMPULSE_15M",
            "CONTINUATION_5M_30M",
            "OI_AND_FUNDING",
            "FUTURE_MOVE_LABELING",
            "DAILY_RESEARCH_SNAPSHOT",
            "SHADOW_ONLY",
        ],
    }


@app.get("/events")
def events(limit: int = 100):
    return store.recent(max(1, min(limit, 500)))


@app.get("/stats")
def stats():
    return store.stats()


@app.get("/research-status")
def research_status():
    return store.research_status()


@app.get("/research-snapshots")
def research_snapshots(limit: int = 30):
    return store.snapshots(max(1, min(limit, 365)))


@app.get("/candidates")
def candidates(limit: int = 50):
    return store.confirmed_candidates(max(1, min(limit, 200)))


@app.get("/historical-status")
def historical_status():
    return _historical_status_with_integrity_gate()


@app.get("/micro-live-readiness")
def micro_live_readiness():
    return _micro_live_readiness()


@app.get("/gate-status")
def gate_status(candidate_limit: int = 20):
    worker = store.worker_health(int(os.getenv("WORKER_STALE_SECONDS", "300")))
    historical = _historical_status_with_integrity_gate()
    research = store.research_status()
    readiness = _micro_live_readiness()
    candidates = store.confirmed_candidates(max(1, min(candidate_limit, 100)))
    return {
        "live_execution": False,
        "worker": worker,
        "historical": historical,
        "research_gate": research.get("research_gate"),
        "micro_live": readiness,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


@app.get("/execution-preflight")
def execution_preflight(equity_usdt: float):
    readiness = _micro_live_readiness()
    eligible = readiness.get("eligible_candidates") or []
    if not readiness.get("ready") or not eligible:
        return {
            "ready": False,
            "reason": "micro_live_gate_not_ready",
            "readiness": readiness,
            "plan": None,
        }

    candidate = eligible[0]
    try:
        constraints = bybit_linear_constraints(candidate["symbol"])
    except Exception as exc:
        return {
            "ready": False,
            "reason": "exchange_constraints_unavailable",
            "error": repr(exc),
            "candidate": candidate,
            "plan": None,
            "execution_enabled": False,
        }

    if constraints.get("status") != "Trading":
        return {
            "ready": False,
            "reason": "instrument_not_trading",
            "candidate": candidate,
            "exchange_constraints": constraints,
            "plan": None,
            "execution_enabled": False,
        }

    plan = build_execution_plan(
        symbol=candidate["symbol"],
        entry_price=float(candidate["signal_price"]),
        equity_usdt=equity_usdt,
        risk_limits=readiness["risk_limits"],
        min_notional_usdt=constraints.get("min_notional_usdt"),
        qty_step=constraints.get("qty_step"),
    )
    return {
        "ready": bool(plan.allowed),
        "candidate": candidate,
        "exchange_constraints": constraints,
        "plan": plan.to_dict(),
        "execution_enabled": False,
    }


@app.get("/promo-candidates")
def promo_candidates(limit: int = 20):
    runtime = store.get_runtime("promo_scan") or {}
    value = runtime.get("value") or {}
    candidates = value.get("candidates") or []
    return {
        "source": value.get("source"),
        "generated_at_ms": value.get("generated_at_ms"),
        "scanned": value.get("scanned"),
        "execution_enabled": False,
        "candidates": candidates[:max(1, min(limit, 50))],
    }


@app.on_event("startup")
def startup_promo_snapshot():
    try:
        promos = scan_promos(limit=50)
        store.set_runtime("promo_scan", promos)
        print("PROMO_STARTUP", {
            "scanned": promos.get("scanned"),
            "top": (promos.get("candidates") or [])[:3],
            "execution_enabled": False,
        }, flush=True)
    except Exception as exc:
        print("PROMO_STARTUP_ERROR", repr(exc), flush=True)

    try:
        funding = funding_balances(("USDT", "USDC", "USD1", "MNT", "ETH", "BTC", "SOL", "XRP", "DOGE", "BNB"))
        store.set_runtime("promo_account_snapshot", {
            "funding": funding,
            "execution_enabled": False,
        })
        print("FUNDING_BALANCE", funding, flush=True)
    except Exception as exc:
        print("FUNDING_BALANCE_ERROR", repr(exc), flush=True)


@app.get("/dry-run/spot")
def dry_run_spot(symbol: str = "SOLUSDT", simulated_balance_usdt: float = 15.0):
    return dry_run_suite(symbol=symbol, simulated_balance_usdt=simulated_balance_usdt)


@app.on_event("startup")
def startup_spot_dry_run():
    try:
        result = dry_run_suite("SOLUSDT", 15.0)
        store.set_runtime("spot_dry_run_suite", result)
        print("SPOT_DRY_RUN", result, flush=True)
    except Exception as exc:
        print("SPOT_DRY_RUN_ERROR", repr(exc), flush=True)


@app.get("/shadow-pnl")
def shadow_pnl():
    row = store.get_runtime("shadow_portfolio_v1")
    if row is None or not isinstance(row.get("value"), dict):
        return {
            "mode": "LIVE_SHADOW_REAL_MARKET",
            "status": "not_started",
            "starting_equity_usdt": float(os.getenv("SHADOW_START_EQUITY_USDT", "15")),
        }
    return shadow_summary(row["value"])


@app.get("/v2/status")
def v2_status():
    row = store.get_runtime("v2_scan")
    value = None if row is None else row.get("value")
    if not isinstance(value, dict):
        return {
            "engine": "MomentumAgentV2",
            "mode": "SHADOW_ONLY",
            "status": "waiting_for_first_scan",
            "live_execution": False,
        }
    return {
        **value,
        "live_execution": False,
    }


@app.get("/v2/shadow-pnl")
def v2_shadow_pnl():
    row = store.get_runtime("v2_shadow_portfolio")
    if row is None or not isinstance(row.get("value"), dict):
        return {
            "mode": "V2_LIVE_SHADOW_1M_PATH",
            "status": "not_started",
            "starting_equity_usdt": float(os.getenv("V2_SHADOW_START_EQUITY_USDT", "15")),
            "live_execution": False,
        }
    return {
        **v2_shadow_summary(row["value"]),
        "live_execution": False,
    }


@app.get("/v2/backtest-status")
def v2_backtest_status():
    state_row = store.get_runtime("v2_backtest_state")
    summary_row = store.get_runtime("v2_backtest_summary")
    error_row = store.get_runtime("v2_backtest_error")
    state = None if state_row is None else state_row.get("value")
    summary = None if summary_row is None else summary_row.get("value")
    error = None if error_row is None else error_row.get("value")
    return {
        "engine": "MomentumAgentV2",
        "mode": "HISTORICAL_SPOT_1M_PATH",
        "state": state,
        "summary": summary,
        "error": error,
        "live_execution": False,
    }


@app.get("/v2/readiness")
def v2_readiness():
    return evaluate_v2_readiness(store)


@app.get("/v22/status")
def v22_status():
    row = store.get_runtime("v22_fast_scan")
    value = None if row is None else row.get("value")
    if not isinstance(value, dict):
        return {
            "engine": "MomentumAgentV2.2",
            "status": "waiting_for_first_fast_scan",
            "live_execution": False,
        }
    return value


@app.get("/v22/shadow-pnl")
def v22_shadow_pnl():
    row = store.get_runtime("v22_shadow_portfolio")
    if row is None or not isinstance(row.get("value"), dict):
        return {
            "mode": "V2.2_SENSITIVE_RUNNER_SHADOW",
            "status": "not_started",
            "starting_equity_usdt": float(os.getenv("V22_SHADOW_START_EQUITY_USDT", "15")),
            "live_execution": False,
        }
    return v22_shadow_summary(row["value"])


@app.get("/v22/replay-status")
def v22_replay_status():
    state_row = store.get_runtime("v22_runner_replay_state")
    summary_row = store.get_runtime("v22_runner_replay_summary")
    error_row = store.get_runtime("v22_runner_replay_error")
    return {
        "engine": "MomentumAgentV2.2",
        "mode": "RUNNER_AB_REPLAY",
        "state": None if state_row is None else state_row.get("value"),
        "summary": None if summary_row is None else summary_row.get("value"),
        "error": None if error_row is None else error_row.get("value"),
        "live_execution": False,
    }


@app.get("/v22/readiness")
def v22_readiness():
    return evaluate_v22_readiness(store)


@app.get("/v22/flow-stats")
def v22_flow_stats():
    return store.v22_flow_snapshot_stats()


@app.get("/v24/status")
def v24_status():
    stream = store.get_runtime("v24_stream_status")
    micro = store.get_runtime("v24_microstructure_latest")
    return {
        "engine": "MomentumAgentV2.4",
        "stream": None if stream is None else stream.get("value"),
        "microstructure": None if micro is None else micro.get("value"),
        "live_execution": False,
    }


@app.get("/v24/microstructure")
def v24_microstructure():
    row = store.get_runtime("v24_microstructure_latest")
    if row is None or not isinstance(row.get("value"), dict):
        return {
            "engine": "MomentumAgentV2.4",
            "status": "waiting_for_websocket_data",
            "live_execution": False,
        }
    return row["value"]


@app.get("/v24/shadow-pnl")
def v24_shadow_pnl():
    row = store.get_runtime("v24_event_shadow_summary")
    if row is None or not isinstance(row.get("value"), dict):
        row = store.get_runtime("v24_event_shadow")
        value = None if row is None else row.get("value")
        return {
            "engine": "MomentumAgentV2.4",
            "mode": "EVENT_DRIVEN_SHADOW",
            "status": "waiting_for_stream",
            "state": value,
            "live_execution": False,
        }
    return row["value"]
