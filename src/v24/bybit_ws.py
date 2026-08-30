from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict

from websockets.asyncio.client import connect

from .book import LocalOrderBook
from .features import MicrostructureFeatureEngine


SPOT_WS = os.getenv("BYBIT_SPOT_WS_URL", "wss://stream.bybit.com/v5/public/spot")


class BybitSpotStream:
    def __init__(self, symbols, on_features=None):
        self.symbols = list(dict.fromkeys(symbols))
        self.on_features = on_features
        self.books = {s: LocalOrderBook(s) for s in self.symbols}
        self.features = MicrostructureFeatureEngine()
        self.message_counts = defaultdict(int)
        self.last_message_ms = 0
        self.reconnects = 0
        self.connected = False
        self.last_error = None

    def _topics(self):
        topics = []
        depth = int(os.getenv("V24_ORDERBOOK_DEPTH", "50"))
        for symbol in self.symbols:
            topics.append(f"orderbook.{depth}.{symbol}")
            topics.append(f"publicTrade.{symbol}")
        return topics

    async def _subscribe(self, ws):
        topics = self._topics()
        chunk = max(1, int(os.getenv("V24_SUBSCRIBE_CHUNK", "10")))
        for i in range(0, len(topics), chunk):
            await ws.send(json.dumps({"op": "subscribe", "args": topics[i:i+chunk]}))
            await asyncio.sleep(0.05)

    async def _heartbeat(self, ws):
        interval = max(10, int(os.getenv("V24_WS_PING_SECONDS", "20")))
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"op": "ping"}))

    async def _handle(self, raw):
        msg = json.loads(raw)
        self.last_message_ms = int(time.time() * 1000)

        topic = str(msg.get("topic") or "")
        if not topic:
            return

        if topic.startswith("orderbook."):
            data = msg.get("data") or {}
            symbol = str(data.get("s") or topic.split(".")[-1])
            book = self.books.get(symbol)
            if book is None:
                return
            book.apply(str(msg.get("type") or "delta"), data, msg.get("ts"))
            self.message_counts["orderbook"] += 1
            return

        if topic.startswith("publicTrade."):
            rows = msg.get("data") or []
            for t in rows:
                symbol = str(t.get("s") or topic.split(".")[-1])
                if symbol not in self.books:
                    continue
                trade = {
                    "time": int(t.get("T") or msg.get("ts") or self.last_message_ms),
                    "price": float(t.get("p") or 0.0),
                    "size": float(t.get("v") or 0.0),
                    "side": str(t.get("S") or ""),
                    "trade_id": t.get("i"),
                    "seq": t.get("seq"),
                }
                self.features.on_trade(symbol, trade)
                self.message_counts["trade"] += 1

    async def _feature_loop(self):
        interval = max(0.25, float(os.getenv("V24_FEATURE_INTERVAL_SECONDS", "1.0")))
        while True:
            now_ms = int(time.time() * 1000)
            for symbol, book in self.books.items():
                snap = book.snapshot(20)
                if not snap.get("ready"):
                    continue
                feature = self.features.compute(symbol, snap, now_ms)
                if self.on_features is not None:
                    result = self.on_features(symbol, feature)
                    if asyncio.iscoroutine(result):
                        await result
            await asyncio.sleep(interval)

    async def run_once(self):
        async with connect(
            SPOT_WS,
            ping_interval=None,
            close_timeout=5,
            max_queue=4096,
            open_timeout=15,
        ) as ws:
            self.connected = True
            self.last_error = None
            await self._subscribe(ws)
            heartbeat = asyncio.create_task(self._heartbeat(ws))
            feature_loop = asyncio.create_task(self._feature_loop())
            try:
                async for raw in ws:
                    await self._handle(raw)
            finally:
                heartbeat.cancel()
                feature_loop.cancel()
                self.connected = False

    async def run_forever(self):
        delay = 1.0
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.connected = False
                self.last_error = repr(exc)
                self.reconnects += 1
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, 30.0)
            else:
                delay = 1.0

    def status(self):
        return {
            "connected": self.connected,
            "symbols": self.symbols,
            "message_counts": dict(self.message_counts),
            "last_message_ms": self.last_message_ms,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }
