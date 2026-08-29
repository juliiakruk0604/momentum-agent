from __future__ import annotations

import os
from fastapi import FastAPI, HTTPException

from src.store import SignalStore
from src.execution.preflight import build_execution_plan
from src.execution.exchange_constraints import bybit_linear_constraints

app = FastAPI(title="Momentum Research Agent", version="3.3.4")
store = SignalStore()


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
    return store.micro_live_readiness()


@app.get("/gate-status")
def gate_status(candidate_limit: int = 20):
    worker = store.worker_health(int(os.getenv("WORKER_STALE_SECONDS", "300")))
    historical = store.historical_status()
    research = store.research_status()
    readiness = store.micro_live_readiness()
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
    readiness = store.micro_live_readiness()
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
