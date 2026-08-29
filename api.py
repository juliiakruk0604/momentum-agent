from __future__ import annotations

import os
from fastapi import FastAPI, HTTPException

from src.store import SignalStore

app = FastAPI(title="Momentum Research Agent", version="3.1.0")
store = SignalStore()


@app.get("/health")
def health():
    try:
        db_ok = store.ping()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database_unhealthy: {exc}")
    return {
        "status": "ok" if db_ok else "degraded",
        "mode": os.getenv("MODE", "shadow"),
        "live_execution": False,
        "database": store.backend,
        "worker": store.get_runtime("worker_heartbeat"),
        "last_worker_error": store.get_runtime("worker_error"),
        "pipeline": [
            "EARLY_IMPULSE_15M",
            "CONTINUATION_5M_30M",
            "OI_AND_FUNDING",
            "FUTURE_MOVE_LABELING",
            "SHADOW_ONLY",
        ],
    }


@app.get("/events")
def events(limit: int = 100):
    return store.recent(max(1, min(limit, 500)))


@app.get("/stats")
def stats():
    return store.stats()
