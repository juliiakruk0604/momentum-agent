from __future__ import annotations

import os
from fastapi import FastAPI, HTTPException

from src.store import SignalStore

app = FastAPI(title="Momentum Research Agent", version="3.3.1")
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
