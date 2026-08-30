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
from src.v24.okx_ws import OKXTradeStream, available_symbols as okx_available_symbols
from src.v24.sequence import SequenceFeatureEngine
from src.v25.hybrid import V25HybridShadow


def _latest_v22_map(store):
    row = store.get_runtime("v22_fast_scan")
    value = None if row is None else row.get("value")
    candidates = [] if not isinstance(value, dict) else (value.get("candidates") or [])
    return {
        str(x.get("symbol")): x
        for x in candidates
        if x.get("symbol")
    }


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
    okx_symbols = okx_available_symbols(symbols)

    runtime = V24Runtime(store, symbols)
    sequence_engine = SequenceFeatureEngine()
    hybrid_shadow = V25HybridShadow(store)
    last_feature_archive_ms = 0
    last_price_tick_ms = 0
    last_price_prune_ms = 0
    stream = BybitSpotStream(symbols, on_features=runtime.on_features)
    linear_stream = BybitLinearContextStream(linear_symbols)
    binance_stream = BinanceTradeStream(binance_symbols)
    okx_stream = OKXTradeStream(okx_symbols)

    print("V24_STREAM_START", json.dumps({
        "symbols": symbols,
        "linear_symbols": linear_symbols,
        "binance_symbols": binance_symbols,
        "okx_symbols": okx_symbols,
        "live_execution": False,
    }, default=str), flush=True)

    async def status_loop():
        while True:
            status = stream.status()
            status["generated_at_ms"] = int(time.time() * 1000)
            status["linear_stream"] = linear_stream.status()
            status["binance_stream"] = binance_stream.status()
            status["okx_stream"] = okx_stream.status()
            store.set_runtime("v24_stream_status", status)
            print("V24_STREAM_STATUS", json.dumps(status, default=str), flush=True)
            await asyncio.sleep(max(10, int(os.getenv("V24_STATUS_SECONDS", "30"))))

    async def parity_loop():
        interval = max(10, int(os.getenv("V24_PARITY_SECONDS", "30")))
        tolerance_bps = float(os.getenv("V24_PARITY_MAX_BPS", "5"))
        max_age_ms = int(float(os.getenv("V24_MAX_EVENT_AGE_SECONDS", "3")) * 1000)
        while True:
            now_ms = int(time.time() * 1000)
            checks = []
            healthy = True
            for symbol in symbols[: min(len(symbols), int(os.getenv("V24_PARITY_SYMBOLS", "4")))]:
                local = stream.books.get(symbol)
                local_snap = {} if local is None else local.snapshot(5)
                try:
                    rest = provider.orderbook(symbol, limit=25)
                    rb = rest.get("bids") or []
                    ra = rest.get("asks") or []
                    rest_bid = float(rb[0][0]) if rb else 0.0
                    rest_ask = float(ra[0][0]) if ra else 0.0
                except Exception as exc:
                    checks.append({"symbol":symbol,"ok":False,"error":repr(exc)[:120]})
                    healthy = False
                    continue
                local_bid = float(local_snap.get("best_bid") or 0.0)
                local_ask = float(local_snap.get("best_ask") or 0.0)
                bid_bps = 9999.0 if rest_bid <= 0 else abs(local_bid / rest_bid - 1.0) * 10000.0
                ask_bps = 9999.0 if rest_ask <= 0 else abs(local_ask / rest_ask - 1.0) * 10000.0
                age_ms = now_ms - int(local_snap.get("exchange_ts_ms") or 0)
                ok = (
                    bool(local_snap.get("ready"))
                    and bid_bps <= tolerance_bps
                    and ask_bps <= tolerance_bps
                    and 0 <= age_ms <= max_age_ms
                )
                if not ok:
                    healthy = False
                checks.append({
                    "symbol":symbol,
                    "ok":ok,
                    "bid_diff_bps":bid_bps,
                    "ask_diff_bps":ask_bps,
                    "event_age_ms":age_ms,
                })
            payload = {
                "healthy":healthy,
                "checked_at_ms":now_ms,
                "checks":checks,
                "max_diff_bps":tolerance_bps,
                "max_event_age_ms":max_age_ms,
            }
            store.set_runtime("v24_stream_parity", payload)
            if not healthy:
                print("V24_PARITY_DEGRADED", json.dumps(payload, default=str), flush=True)
            await asyncio.sleep(interval)

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
                base_map = _latest_v22_map(store)
                for feature in ranked:
                    symbol = feature.get("symbol")
                    context = linear_stream.context(symbol, now_ms)
                    bctx = binance_stream.context(symbol, now_ms)
                    octx = okx_stream.context(symbol, now_ms)
                    b1 = float(((bctx.get("trade_1s") or {}).get("price_move_pct") or 0.0))
                    b5 = float(((bctx.get("trade_5s") or {}).get("price_move_pct") or 0.0))
                    o1 = float(((octx.get("trade_1s") or {}).get("price_move_pct") or 0.0))
                    o5 = float(((octx.get("trade_5s") or {}).get("price_move_pct") or 0.0))
                    y1 = float(feature.get("price_move_1s_pct") or 0.0)
                    y5 = float(feature.get("price_move_5s_pct") or 0.0)
                    ext1 = [x for x,ok in ((b1,bool(bctx.get("available"))),(o1,bool(octx.get("available")))) if ok]
                    ext5 = [x for x,ok in ((b5,bool(bctx.get("available"))),(o5,bool(octx.get("available")))) if ok]
                    consensus1 = sum(ext1)/len(ext1) if ext1 else 0.0
                    consensus5 = sum(ext5)/len(ext5) if ext5 else 0.0
                    cross = {
                        "binance_available": bool(bctx.get("available")),
                        "okx_available": bool(octx.get("available")),
                        "binance_trade_1s": bctx.get("trade_1s"),
                        "binance_trade_5s": bctx.get("trade_5s"),
                        "okx_trade_1s": octx.get("trade_1s"),
                        "okx_trade_5s": octx.get("trade_5s"),
                        "external_consensus_move_1s_pct": consensus1,
                        "external_consensus_move_5s_pct": consensus5,
                        "external_minus_bybit_move_1s_pct": consensus1 - y1,
                        "external_minus_bybit_move_5s_pct": consensus5 - y5,
                        "auto_weight_in_signal": False,
                    }
                    enriched_item = {
                        **feature,
                        "perp_context": context,
                        "cross_exchange": cross,
                        "base_momentum": base_map.get(str(symbol)),
                    }
                    enriched.append(sequence_engine.enrich(enriched_item, now_ms))
                ranked = enriched

                nonlocal last_feature_archive_ms, last_price_tick_ms, last_price_prune_ms

                price_tick_interval_ms = int(
                    max(0.5, float(os.getenv("V24_PRICE_TICK_SECONDS", "1.0"))) * 1000
                )
                if now_ms - last_price_tick_ms >= price_tick_interval_ms:
                    written = store.insert_v24_price_ticks_batch(now_ms, ranked)
                    if written:
                        last_price_tick_ms = now_ms
                        if store.get_runtime("v24_price_tick_started") is None:
                            store.set_runtime("v24_price_tick_started", {
                                "started_ms": now_ms,
                                "symbols": written,
                                "version": "price_tick_v2",
                            })
                    if now_ms - last_price_prune_ms >= 60_000:
                        retention_hours = max(
                            2,
                            int(os.getenv("V24_PRICE_TICK_RETENTION_HOURS", "6")),
                        )
                        store.prune_v24_price_ticks(
                            now_ms - retention_hours * 3600 * 1000
                        )
                        last_price_prune_ms = now_ms

                archive_interval_ms = int(
                    max(1.0, float(os.getenv("V24_FEATURE_ARCHIVE_SECONDS", "5"))) * 1000
                )
                if now_ms - last_feature_archive_ms >= archive_interval_ms:
                    regime_for_archive = runtime.current_regime()
                    for item in ranked:
                        store.upsert_v24_feature_snapshot({
                            **item,
                            "snapshot_ms": now_ms,
                            "regime": regime_for_archive,
                        })
                    last_feature_archive_ms = now_ms
                    stats = store.v24_feature_snapshot_stats()
                    store.set_runtime("v24_feature_snapshot_stats", stats)

                    retention_hours = max(
                        24,
                        int(os.getenv("V24_FEATURE_RETENTION_HOURS", "168")),
                    )
                    prune_before = now_ms - retention_hours * 3600 * 1000
                    store.prune_v24_feature_snapshots(prune_before)

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
                parity_row = store.get_runtime("v24_stream_parity")
                parity = None if parity_row is None else parity_row.get("value")
                regime = runtime.current_regime()
                if isinstance(parity, dict) and parity.get("healthy") is False:
                    regime = "DATA_DEGRADED"
                summary = runtime.shadow.process(ranked, regime)
                store.set_runtime("v24_event_shadow_summary", summary)

                hybrid = hybrid_shadow.process(ranked, regime)
                store.set_runtime("v25_hybrid_shadow_summary", hybrid)
                hybrid_fp = json.dumps({
                    "armed": hybrid.get("armed"),
                    "open": None if not hybrid.get("open_position") else hybrid["open_position"].get("symbol"),
                    "last_action": hybrid.get("last_action"),
                    "equity": hybrid.get("current_equity_usdt"),
                }, sort_keys=True, default=str)
                prev_hybrid = store.get_runtime("v25_hybrid_last_print")
                prev_hybrid_fp = None if prev_hybrid is None else prev_hybrid.get("value")
                if hybrid_fp != prev_hybrid_fp:
                    print("V25_HYBRID_STATE", json.dumps(hybrid, default=str), flush=True)
                    store.set_runtime("v25_hybrid_last_print", hybrid_fp)
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
        okx_stream.run_forever(),
        status_loop(),
        parity_loop(),
        decision_loop(),
    )


def main():
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
