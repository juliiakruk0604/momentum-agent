from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque

import requests
from websockets.asyncio.client import connect


BINANCE_WS = os.getenv("BINANCE_WS_URL", "wss://stream.binance.com:9443/ws")
BINANCE_REST = os.getenv("BINANCE_REST_URL", "https://api.binance.com")


def available_symbols(symbols):
    try:
        r = requests.get(
            BINANCE_REST + "/api/v3/exchangeInfo",
            timeout=float(os.getenv("V24_BINANCE_REST_TIMEOUT", "8")),
        )
        r.raise_for_status()
        payload = r.json()
        trading = {
            str(x.get("symbol"))
            for x in payload.get("symbols") or []
            if x.get("status") == "TRADING"
        }
        return [s for s in symbols if s in trading]
    except Exception:
        return []


class BinanceTradeStream:
    def __init__(self, symbols):
        self.symbols = list(dict.fromkeys(symbols))
        self.trades = defaultdict(deque)
        self.connected = False
        self.reconnects = 0
        self.last_error = None
        self.last_message_ms = 0
        self.message_counts = 0

    async def _subscribe(self, ws):
        if not self.symbols:
            return
        params = [f"{s.lower()}@aggTrade" for s in self.symbols]
        await ws.send(json.dumps({
            "method": "SUBSCRIBE",
            "params": params,
            "id": 1,
        }))

    @staticmethod
    def _trim(dq, cutoff):
        while dq and int(dq[0]["time"]) < cutoff:
            dq.popleft()

    async def _handle(self, raw):
        msg = json.loads(raw)
        if "result" in msg and "id" in msg:
            return
        data = msg.get("data") if isinstance(msg.get("data"), dict) else msg
        if str(data.get("e") or "") != "aggTrade":
            return
        symbol = str(data.get("s") or "")
        if symbol not in self.symbols:
            return
        price = float(data.get("p") or 0.0)
        size = float(data.get("q") or 0.0)
        # m=True means buyer is maker, therefore aggressive taker was Sell.
        side = "Sell" if bool(data.get("m")) else "Buy"
        ts = int(data.get("T") or data.get("E") or int(time.time() * 1000))
        row = {
            "time": ts,
            "price": price,
            "size": size,
            "side": side,
            "notional": price * size,
        }
        dq = self.trades[symbol]
        dq.append(row)
        self._trim(dq, ts - 120_000)
        self.last_message_ms = int(time.time() * 1000)
        self.message_counts += 1

    async def run_once(self):
        if not self.symbols:
            await asyncio.sleep(3600)
            return
        async with connect(
            BINANCE_WS,
            close_timeout=5,
            max_queue=4096,
            open_timeout=15,
        ) as ws:
            self.connected = True
            self.last_error = None
            await self._subscribe(ws)
            try:
                async for raw in ws:
                    await self._handle(raw)
            finally:
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
    def _window(rows, now_ms, seconds):
        cutoff = now_ms - int(seconds * 1000)
        selected = [x for x in rows if int(x["time"]) >= cutoff]
        buy = sum(float(x["notional"]) for x in selected if x["side"] == "Buy")
        sell = sum(float(x["notional"]) for x in selected if x["side"] == "Sell")
        total = buy + sell
        first = float(selected[0]["price"]) if selected else None
        last = float(selected[-1]["price"]) if selected else None
        move = 0.0 if not first or not last or first <= 0 else (last / first - 1.0) * 100.0
        return {
            "trade_count": len(selected),
            "total_notional": total,
            "buy_ratio": 0.5 if total <= 0 else buy / total,
            "cvd_ratio": 0.0 if total <= 0 else (buy - sell) / total,
            "price_move_pct": move,
            "last_price": last,
        }

    def context(self, symbol, now_ms=None):
        now_ms = int(now_ms or time.time() * 1000)
        rows = list(self.trades.get(symbol) or [])
        latest_trade_ms = int(rows[-1]["time"]) if rows else 0
        age_ms = max(0, now_ms - latest_trade_ms) if latest_trade_ms else None
        max_context_age_ms = max(1000, int(os.getenv("V24_BINANCE_CONTEXT_MAX_AGE_MS", "5000")))
        available = bool(rows) and age_ms is not None and age_ms <= max_context_age_ms
        w1 = self._window(rows, now_ms, 1) if available else self._window([], now_ms, 1)
        w5 = self._window(rows, now_ms, 5) if available else self._window([], now_ms, 5)
        return {
            "available": available,
            "stale": bool(rows) and not available,
            "symbol": symbol,
            "latest_trade_ms": latest_trade_ms or None,
            "age_ms": age_ms,
            "max_context_age_ms": max_context_age_ms,
            "trade_1s": w1,
            "trade_5s": w5,
        }

    def status(self):
        return {
            "connected": self.connected,
            "symbols": self.symbols,
            "reconnects": self.reconnects,
            "last_error": self.last_error,
            "last_message_ms": self.last_message_ms,
            "message_counts": self.message_counts,
        }
