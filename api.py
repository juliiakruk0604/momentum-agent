from __future__ import annotations

import os
from fastapi import FastAPI, HTTPException

from src.store import SignalStore
from src.execution.preflight import build_execution_plan
from src.execution.exchange_constraints import bybit_linear_constraints

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


@app.get("/bybit/account-test")
def bybit_account_test():
    try:
        return account_diagnostic()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"bybit_account_test_failed: {exc}")


@app.on_event("startup")
def startup_bybit_diagnostic():
    if not os.getenv("BYBIT_API_KEY") or not os.getenv("BYBIT_API_SECRET"):
        print("BYBIT_DIAGNOSTIC not_configured", flush=True)
        return
    try:
        result = account_diagnostic()
        safe = {
            "connected": result.get("connected"),
            "safe_permissions": result.get("safe_permissions"),
            "read_only": result.get("read_only"),
            "spot_trade_enabled": result.get("spot_trade_enabled"),
            "forbidden_permissions": result.get("forbidden_permissions"),
            "account_type": result.get("account_type"),
            "total_equity_usd": result.get("total_equity_usd"),
            "total_available_balance_usd": result.get("total_available_balance_usd"),
            "execution_enabled": False,
        }
        print("BYBIT_DIAGNOSTIC", safe, flush=True)
    except Exception as exc:
        print("BYBIT_DIAGNOSTIC_ERROR", repr(exc), flush=True)
