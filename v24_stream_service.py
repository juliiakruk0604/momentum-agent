from __future__ import annotations

import asyncio
import json
import os
import time

from src.store import SignalStore
from src.v2.provider import BybitV2Provider
from src.v24.bybit_ws import BybitSpotStream
from src.v24.challenger import V24EventShadow
from src.v24.linear_ws import BybitLinearContextStream
from src.v24.binance_ws import BinanceTradeStream, available_symbols as binance_available_symbols


class V24Runtime:
    def __init__(self, store, symbols):
        self.store = store
        self.symbols = symbols
        self.shadow = V24EventShadow(store)
        self.latest = {}
        self.last_runtime_write = 0.0
        self.last_archive_write = 0.0

    def current_regime(self):
        fast = self.store.get_runtime("v22_fast_scan")
        value = None if fast is None else fast.get("value")
        if isinstance(value, dict):
            regime = (value.get("regime") or {}).get("name")
            if regime:
                return str(regime)
        slow = self.store.get_runtime("v2_scan")
        value = None if slow is None else slow.get("value")
        if isinstance(value, dict):
            regime = (value.get("regime") or {}).get("name")
            if regime:
                return str(regime)
        return "UNKNOWN"

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

    linear_instruments = provider.instruments("linear")
    linear_set = {
        x.get("symbol")
        for x in linear_instruments
        if x.get("status") == "Trading" and x.get("quoteCoin") == "USDT"
    }
    linear_symbols = [s for s in symbols if s in linear_set]

    binance_symbols = binance_available_symbols(symbols)

    runtime = V24Runtime(store, symbols)
    stream = BybitSpotStream(symbols, on_features=runtime.on_features)
    linear_stream = BybitLinearContextStream(linear_symbols)
    binance_stream = BinanceTradeStream(binance_symbols)

    print("V24_STREAM_START", json.dumps({
        "symbols": symbols,
        "linear_symbols": linear_symbols,
        "binance_symbols": binance_symbols,
        "live_execution": False,
    }), flush=True)

    async def status_loop():
        while True:
            status = stream.status()
            status["generated_at_ms"] = int(time.time() * 1000)
            status["linear_stream"] = linear_stream.status()
            status["binance_stream"] = binance_stream.status()
            store.set_runtime("v24_stream_status", status)
            print("V24_STREAM_STATUS", json.dumps(status, default=str), flush=True)
            await asyncio.sleep(max(10, int(os.getenv("V24_STATUS_SECONDS", "30"))))

    async def decision_loop():
        last_print = None
        while True:
            ranked = sorted(
                runtime.latest.values(),
                key=lambda x: float(x.get("microstructure_score") or 0.0),
                reverse=True,
            )
            if ranked:
                now_ms = int(time.time() * 1000)
                enriched = []
                for feature in ranked:
                    symbol = feature.get("symbol")
                    context = linear_stream.context(symbol, now_ms)
                    bctx = binance_stream.context(symbol, now_ms)
                    b1 = float(((bctx.get("trade_1s") or {}).get("price_move_pct") or 0.0))
                    b5 = float(((bctx.get("trade_5s") or {}).get("price_move_pct") or 0.0))
                    y1 = float(feature.get("price_move_1s_pct") or 0.0)
                    y5 = float(feature.get("price_move_5s_pct") or 0.0)
                    cross = {
                        "binance_available": bool(bctx.get("available")),
                        "binance_trade_1s": bctx.get("trade_1s"),
                        "binance_trade_5s": bctx.get("trade_5s"),
                        "binance_minus_bybit_move_1s_pct": b1 - y1,
                        "binance_minus_bybit_move_5s_pct": b5 - y5,
                        "auto_weight_in_signal": False,
                    }
                    enriched.append({
                        **feature,
                        "perp_context": context,
                        "cross_exchange": cross,
                    })
                ranked = enriched
                store.set_runtime("v24_cross_exchange_top", {
                    "generated_at_ms": now_ms,
                    "top": [
                        {
                            "symbol": x.get("symbol"),
                            "microstructure_score": x.get("microstructure_score"),
                            "cross_exchange": x.get("cross_exchange"),
                        }
                        for x in ranked[:10]
                    ],
                    "auto_weight_in_signal": False,
                })
                store.set_runtime("v24_perp_context_top", {
                    "generated_at_ms": now_ms,
                    "top": [
                        {
                            "symbol": x.get("symbol"),
                            "microstructure_score": x.get("microstructure_score"),
                            "perp_context": x.get("perp_context"),
                        }
                        for x in ranked[:10]
                    ],
                    "auto_weight_in_signal": False,
                })
                summary = runtime.shadow.process(ranked, runtime.current_regime())
                store.set_runtime("v24_event_shadow_summary", summary)
                fingerprint = json.dumps({
                    "armed": summary.get("armed"),
                    "open": None if not summary.get("open_position") else summary["open_position"].get("symbol"),
                    "last_action": summary.get("last_action"),
                    "equity": summary.get("current_equity_usdt"),
                }, sort_keys=True, default=str)
                if fingerprint != last_print:
                    print("V24_SHADOW_STATE", json.dumps(summary, default=str), flush=True)
                    last_print = fingerprint
            await asyncio.sleep(max(0.5, float(os.getenv("V24_DECISION_INTERVAL_SECONDS", "1.0"))))

    await asyncio.gather(
        stream.run_forever(),
        linear_stream.run_forever(),
        binance_stream.run_forever(),
        status_loop(),
        decision_loop(),
    )


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
