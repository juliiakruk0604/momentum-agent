"""Single-container starter for Railway.

By default it runs the scanner worker in a background thread and FastAPI in the
foreground. Set SERVICE_MODE=api or SERVICE_MODE=worker to split them into
separate services later without changing the codebase.
"""
from __future__ import annotations

import logging
import os
import threading
import time

import uvicorn


logger = logging.getLogger(__name__)


def historical_backfill_integrity(store) -> dict:
    runtime = store.get_runtime("historical_backfill_state") or {}
    state = runtime.get("value") or {}
    symbols = sorted({str(symbol) for symbol in (state.get("unresolved_retry_symbols") or []) if symbol})
    retry_attempts = state.get("retry_attempts") or {}
    max_retry_attempts = max(1, int(os.getenv("HISTORICAL_BACKFILL_RETRY_ATTEMPTS", "2")))
    exhausted = [
        symbol for symbol in symbols
        if int(retry_attempts.get(symbol, 0)) >= max_retry_attempts
    ]
    retryable = [symbol for symbol in symbols if symbol not in set(exhausted)]
    return {
        "unresolved_symbols": symbols,
        "unresolved_count": len(symbols),
        "retryable_count": len(retryable),
        "exhausted_count": len(exhausted),
    }


def historical_backfill_unresolved_symbols(store) -> list[str]:
    return historical_backfill_integrity(store)["unresolved_symbols"]


def install_micro_live_integrity_guard(worker_module):
    original = worker_module.process_micro_live

    def guarded_process_micro_live(store):
        integrity = historical_backfill_integrity(store)
        if integrity["unresolved_count"]:
            reason = (
                "historical_backfill_retry_budget_exhausted"
                if integrity["retryable_count"] == 0 and integrity["exhausted_count"] > 0
                else "historical_backfill_has_unresolved_runs"
            )
            return {
                "action": "NO_TRADE",
                "reason": reason,
                "unresolved_count": integrity["unresolved_count"],
                "retryable_count": integrity["retryable_count"],
                "exhausted_count": integrity["exhausted_count"],
            }
        return original(store)

    worker_module.process_micro_live = guarded_process_micro_live


def run_worker():
    try:
        import worker

        install_micro_live_integrity_guard(worker)
        worker.main()
    except BaseException:
        logger.exception("market worker terminated unexpectedly")
        os._exit(1)


def worker_should_restart(health: dict, elapsed_seconds: float, stale_seconds: int) -> bool:
    status = str((health or {}).get("status") or "")
    if status == "stale":
        return True
    return status == "starting" and elapsed_seconds > float(stale_seconds)


def monitor_worker():
    from src.store import SignalStore

    stale_seconds = max(60, int(os.getenv("WORKER_STALE_SECONDS", "300")))
    check_interval = max(15, min(60, stale_seconds // 4))
    store = SignalStore()
    started = time.monotonic()
    while True:
        health = store.worker_health(stale_seconds)
        elapsed = time.monotonic() - started
        if worker_should_restart(health, elapsed, stale_seconds):
            logger.critical("market worker heartbeat stalled: %s", health)
            os._exit(1)
        time.sleep(check_interval)


def main():
    mode = os.getenv("SERVICE_MODE", "all").lower()
    if mode == "worker":
        run_worker()
        return
    if mode == "research":
        from research_service import main as run_research
        run_research()
        return
    if mode == "v2-market":
        from v2_market_service import main as run_v2_market
        run_v2_market()
        return
    if mode == "v24-stream":
        from v24_stream_service import main as run_v24_stream
        run_v24_stream()
        return
    if mode == "all":
        t = threading.Thread(target=run_worker, name="market-worker", daemon=True)
        t.start()
        watchdog = threading.Thread(target=monitor_worker, name="market-worker-watchdog", daemon=True)
        watchdog.start()
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("api:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
