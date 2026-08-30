"""Single-container starter for Railway.

By default it runs the scanner worker in a background thread and FastAPI in the
foreground. Set SERVICE_MODE=api or SERVICE_MODE=worker to split them into
separate services later without changing the codebase.
"""
from __future__ import annotations

import logging
import os
import threading

import uvicorn


logger = logging.getLogger(__name__)


def run_worker():
    try:
        from worker import main

        main()
    except BaseException:
        logger.exception("market worker terminated unexpectedly")
        os._exit(1)


def main():
    mode = os.getenv("SERVICE_MODE", "all").lower()
    if mode == "worker":
        run_worker()
        return
    if mode == "all":
        t = threading.Thread(target=run_worker, name="market-worker", daemon=True)
        t.start()
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run("api:app", host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    main()
