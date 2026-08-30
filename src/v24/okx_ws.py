from __future__ import annotations

import asyncio
import json
import os
import time
from collections import defaultdict, deque

import requests
from websockets.asyncio.client import connect


OKX_WS = os.getenv("OKX_WS_URL", "wss://ws.okx.com:8443/ws/v5/public")
OKX_REST = os.getenv("OKX_REST_URL", "https://www.okx.com")


def _inst_id(symbol: str) -> str:
    if not symbol.endswith("USDT"):
        return symbol
    return symbol[:-4] + "-USDT"


def available_symbols(symbols):
    try:
        r = requests.get(
            OKX_REST + "/api/v5/public/instruments",
            params={"instType":"SPOT"},
            timeout=float(os.getenv("V24_OKX_REST_TIMEOUT", "8")),
        )
        r.raise_for_status()
        payload = r.json()
        trading = {
            str(x.get("instId"))
            for x in payload.get("data") or []
            if str(x.get("state") or "").lower() in ("live","trading")
        }
        return [s for s in symbols if _inst_id(s) in trading]
    except Exception:
        return []


class OKXTradeStream:
    def __init__(self, symbols):
        self.symbols = list(dict.fromkeys(symbols))
        self.inst_to_symbol = {_inst_id(s): s for s in self.symbols}
        self.trades = defaultdict(deque)
        self.connected = False
        self.reconnects = 0
        self.last_error = None
        self.last_message_ms = 0
        self.message_counts = 0

    async def _subscribe(self, ws):
        if not self.symbols:
            return
        args = [{"channel":"trades","instId":_inst_id(s)} for s in self.symbols]
        await ws.send(json.dumps({"op":"subscribe","args":args}))

    @staticmethod
    def _trim(dq, cutoff):
        while dq and int(dq[0]["time"]) < cutoff:
            dq.popleft()

    async def _handle(self, raw):
        msg = json.loads(raw)
        if msg.get("event") in ("subscribe","error"):
            if msg.get("event") == "error":
                self.last_error = json.dumps(msg)[:300]
            return
        arg = msg.get("arg") or {}
        if arg.get("channel") != "trades":
            return
        for row in msg.get("data") or []:
            inst = str(row.get("instId") or arg.get("instId") or "")
            symbol = self.inst_to_symbol.get(inst)
            if not symbol:
                continue
            try:
                price = float(row.get("px") or 0.0)
                size = float(row.get("sz") or 0.0)
                side = str(row.get("side") or "").capitalize()
                ts = int(row.get("ts") or int(time.time()*1000))
            except Exception:
                continue
            item = {
                "time": ts,
                "price": price,
                "size": size,
                "side": side,
                "notional": price * size,
            }
            dq = self.trades[symbol]
            dq.append(item)
            self._trim(dq, ts - 120_000)
            self.last_message_ms = int(time.time()*1000)
            self.message_counts += 1

    async def run_once(self):
        if not self.symbols:
            await asyncio.sleep(3600)
            return
        async with connect(
            OKX_WS,
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
            "cvd_ratio": 0.0 if total <= 0 else (buy-sell)/total,
            "price_move_pct": move,
            "last_price": last,
        }

    def context(self, symbol, now_ms=None):
        now_ms = int(now_ms or time.time()*1000)
        rows = list(self.trades.get(symbol) or [])
        return {
            "available": bool(rows),
            "symbol": symbol,
            "trade_1s": self._window(rows, now_ms, 1),
            "trade_5s": self._window(rows, now_ms, 5),
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
