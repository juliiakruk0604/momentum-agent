from __future__ import annotations

import asyncio
import json
import os
import time

from src.store import SignalStore
from src.v2.provider import BybitV2Provider
from src.v24.bybit_ws import BybitSpotStream


class V24Runtime:
    def __init__(self, store, symbols):
        self.store = store
        self.symbols = symbols
        self.latest = {}
        self.last_runtime_write = 0.0
        self.last_archive_write = 0.0

    def on_features(self, symbol, feature):
        self.latest[symbol] = feature
        now = time.time()
        runtime_every = max(0.5, float(os.getenv("V24_RUNTIME_WRITE_SECONDS", "1.0")))
        if now - self.last_runtime_write >= runtime_every:
            ranked = sorted(
                self.latest.values(),
                key=lambda x: float(x.get("microstructure_score") or 0.0),
                reverse=True,
            )
            payload = {
                "engine": "MomentumAgentV2.4",
                "mode": "BYBIT_WS_MICROSTRUCTURE_SHADOW",
                "generated_at_ms": int(now * 1000),
                "symbols": len(self.symbols),
                "top": ranked[:10],
                "live_execution": False,
            }
            self.store.set_runtime("v24_microstructure_latest", payload)
            self.last_runtime_write = now

        archive_every = max(5.0, float(os.getenv("V24_ARCHIVE_SECONDS", "10")))
        if now - self.last_archive_write >= archive_every:
            ranked = sorted(
                self.latest.values(),
                key=lambda x: float(x.get("microstructure_score") or 0.0),
                reverse=True,
            )
            self.store.set_runtime(
                "v24_microstructure_archive_head",
                {
                    "generated_at_ms": int(now * 1000),
                    "top": ranked[:5],
                    "note": "rolling-forward sample; historical L2 parity unavailable",
                },
            )
            self.last_archive_write = now


async def main_async():
    store = SignalStore()
    provider = BybitV2Provider()
    symbols = provider.liquid_spot_usdt_symbols(
        limit=int(os.getenv("V24_UNIVERSE_LIMIT", "12")),
        min_turnover=float(os.getenv("V24_MIN_TURNOVER_USDT", "10000000")),
    )
    if not symbols:
        raise RuntimeError("v24_universe_empty")

    runtime = V24Runtime(store, symbols)
    stream = BybitSpotStream(symbols, on_features=runtime.on_features)

    print("V24_STREAM_START", json.dumps({
        "symbols": symbols,
        "live_execution": False,
    }), flush=True)

    async def status_loop():
        while True:
            status = stream.status()
            status["generated_at_ms"] = int(time.time() * 1000)
            store.set_runtime("v24_stream_status", status)
            print("V24_STREAM_STATUS", json.dumps(status, default=str), flush=True)
            await asyncio.sleep(max(10, int(os.getenv("V24_STATUS_SECONDS", "30"))))

    await asyncio.gather(stream.run_forever(), status_loop())


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
