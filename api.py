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


def _micro_live_readiness():
    readiness = store.micro_live_readiness()
    historical = readiness.get("historical") or store.historical_status()
    reasons = list(readiness.get("reasons") or [])

    if historical.get("status") != "complete":
        if "historical_backfill_not_complete" not in reasons:
            reasons.append("historical_backfill_not_complete")

    backfill_errors = _historical_backfill_errors(historical)
    if backfill_errors > 0 and "historical_backfill_has_errors" not in reasons:
        reasons.append("historical_backfill_has_errors")

    partial_runs = _historical_backfill_partial_runs(historical)
    if partial_runs > 0 and "historical_backfill_has_partial_runs" not in reasons:
        reasons.append("historical_backfill_has_partial_runs")

    if reasons != list(readiness.get("reasons") or []) or historical is not readiness.get("historical"):
        readiness = {
            **readiness,
            "ready": False if reasons else bool(readiness.get("ready")),
            "reasons": reasons,
            "historical": historical,
            "historical_backfill_error_count": backfill_errors,
            "historical_backfill_partial_run_count": partial_runs,
        }
    else:
        readiness = {
            **readiness,
            "historical_backfill_error_count": backfill_errors,
            "historical_backfill_partial_run_count": partial_runs,
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
    return store.historical_status()


@app.get("/micro-live-readiness")
def micro_live_readiness():
    return _micro_live_readiness()


@app.get("/gate-status")
def gate_status(candidate_limit: int = 20):
    worker = store.worker_health(int(os.getenv("WORKER_STALE_SECONDS", "300")))
    historical = store.historical_status()
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
