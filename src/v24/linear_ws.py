from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque

from websockets.asyncio.client import connect


LINEAR_WS = os.getenv("BYBIT_LINEAR_WS_URL", "wss://stream.bybit.com/v5/public/linear")


class BybitLinearContextStream:
    def __init__(self, symbols):
        self.symbols = list(dict.fromkeys(symbols))
        self.tickers = {}
        self.oi_history = defaultdict(deque)
        self.price_history = defaultdict(deque)
        self.liquidations = defaultdict(deque)
        self.connected = False
        self.reconnects = 0
        self.last_message_ms = 0
        self.last_error = None
        self.message_counts = defaultdict(int)

    def _topics(self):
        out = []
        for symbol in self.symbols:
            out.append(f"tickers.{symbol}")
            out.append(f"allLiquidation.{symbol}")
        return out

    async def _subscribe(self, ws):
        topics = self._topics()
        chunk = max(1, int(os.getenv("V24_LINEAR_SUBSCRIBE_CHUNK", "20")))
        for i in range(0, len(topics), chunk):
            await ws.send(json.dumps({"op":"subscribe","args":topics[i:i+chunk]}))
            await asyncio.sleep(0.05)

    async def _heartbeat(self, ws):
        while True:
            await asyncio.sleep(max(10, int(os.getenv("V24_WS_PING_SECONDS", "20"))))
            await ws.send(json.dumps({"op":"ping"}))

    @staticmethod
    def _trim(dq, cutoff_ms):
        while dq and int(dq[0][0]) < cutoff_ms:
            dq.popleft()

    def _record_ticker(self, symbol, data, ts_ms):
        current = self.tickers.setdefault(symbol, {})
        for key, value in data.items():
            if value not in (None, ""):
                current[key] = value
        current["_ts_ms"] = ts_ms

        oi = current.get("openInterestValue")
        if oi not in (None, ""):
            try:
                value = float(oi)
                dq = self.oi_history[symbol]
                dq.append((ts_ms, value))
                self._trim(dq, ts_ms - 120_000)
            except Exception:
                pass

        last = current.get("lastPrice")
        if last not in (None, ""):
            try:
                value = float(last)
                dq = self.price_history[symbol]
                dq.append((ts_ms, value))
                self._trim(dq, ts_ms - 120_000)
            except Exception:
                pass

    def _record_liquidation(self, symbol, row, ts_ms):
        try:
            price = float(row.get("p") or 0.0)
            size = float(row.get("v") or 0.0)
            side = str(row.get("S") or "")
            event_ms = int(row.get("T") or ts_ms)
            notional = price * size
        except Exception:
            return
        dq = self.liquidations[symbol]
        dq.append((event_ms, side, notional))
        self._trim(dq, event_ms - 120_000)

    async def _handle(self, raw):
        msg = json.loads(raw)
        ts_ms = int(msg.get("ts") or int(time.time() * 1000))
        self.last_message_ms = int(time.time() * 1000)
        topic = str(msg.get("topic") or "")
        if topic.startswith("tickers."):
            data = msg.get("data") or {}
            if isinstance(data, list):
                data = data[0] if data else {}
            symbol = str(data.get("symbol") or topic.split(".")[-1])
            if symbol in self.symbols:
                self._record_ticker(symbol, data, ts_ms)
                self.message_counts["ticker"] += 1
        elif topic.startswith("allLiquidation."):
            rows = msg.get("data") or []
            if isinstance(rows, dict):
                rows = [rows]
            for row in rows:
                symbol = str(row.get("s") or topic.split(".")[-1])
                if symbol in self.symbols:
                    self._record_liquidation(symbol, row, ts_ms)
                    self.message_counts["liquidation"] += 1

    async def run_once(self):
        async with connect(
            LINEAR_WS,
            ping_interval=None,
            close_timeout=5,
            max_queue=4096,
            open_timeout=15,
        ) as ws:
            self.connected = True
            self.last_error = None
            await self._subscribe(ws)
            hb = asyncio.create_task(self._heartbeat(ws))
            try:
                async for raw in ws:
                    await self._handle(raw)
            finally:
                hb.cancel()
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

    @staticmethod
    def _change(dq, now_ms, seconds):
        if not dq:
            return 0.0
        current = float(dq[-1][1])
        target = now_ms - int(seconds * 1000)
        old = None
        for ts, value in dq:
            if int(ts) <= target:
                old = float(value)
            else:
                break
        if old is None or old <= 0:
            return 0.0
        return (current / old - 1.0) * 100.0

    def context(self, symbol, now_ms=None):
        now_ms = int(now_ms or time.time() * 1000)
        ticker = self.tickers.get(symbol) or {}
        liqs = list(self.liquidations.get(symbol) or [])

        def liq_window(seconds):
            cutoff = now_ms - int(seconds * 1000)
            long_liq = 0.0
            short_liq = 0.0
            count = 0
            for ts, side, notion in liqs:
                if int(ts) < cutoff:
                    continue
                count += 1
                # Bybit docs: S=Buy => a long position was liquidated.
                if str(side) == "Buy":
                    long_liq += float(notion)
                elif str(side) == "Sell":
                    short_liq += float(notion)
            return {
                "count": count,
                "long_liq_notional": long_liq,
                "short_liq_notional": short_liq,
                "net_short_minus_long": short_liq - long_liq,
            }

        def f(key):
            try:
                return float(ticker.get(key)) if ticker.get(key) not in (None, "") else None
            except Exception:
                return None

        return {
            "available": bool(ticker),
            "symbol": symbol,
            "ticker_ts_ms": ticker.get("_ts_ms"),
            "last_price": f("lastPrice"),
            "mark_price": f("markPrice"),
            "index_price": f("indexPrice"),
            "open_interest": f("openInterest"),
            "open_interest_value": f("openInterestValue"),
            "funding_rate": f("fundingRate"),
            "oi_change_5s_pct": self._change(self.oi_history[symbol], now_ms, 5),
            "oi_change_30s_pct": self._change(self.oi_history[symbol], now_ms, 30),
            "perp_price_change_5s_pct": self._change(self.price_history[symbol], now_ms, 5),
            "perp_price_change_30s_pct": self._change(self.price_history[symbol], now_ms, 30),
            "liquidation_5s": liq_window(5),
            "liquidation_30s": liq_window(30),
        }

    def status(self):
        return {
            "connected": self.connected,
            "symbols": self.symbols,
            "message_counts": dict(self.message_counts),
            "last_message_ms": self.last_message_ms,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
        }
