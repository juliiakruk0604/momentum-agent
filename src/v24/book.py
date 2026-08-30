from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class LocalOrderBook:
    symbol: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    update_id: int | None = None
    seq: int | None = None
    exchange_ts_ms: int | None = None
    ready: bool = False

    @staticmethod
    def _apply_side(side: dict[float, float], updates):
        for raw_price, raw_qty in updates or []:
            price = float(raw_price)
            qty = float(raw_qty)
            if qty <= 0:
                side.pop(price, None)
            else:
                side[price] = qty

    def apply(self, message_type: str, data: dict, ts_ms: int | None = None):
        if message_type == "snapshot":
            self.bids.clear()
            self.asks.clear()
        self._apply_side(self.bids, data.get("b") or [])
        self._apply_side(self.asks, data.get("a") or [])
        if data.get("u") is not None:
            self.update_id = int(data["u"])
        if data.get("seq") is not None:
            self.seq = int(data["seq"])
        if ts_ms is not None:
            self.exchange_ts_ms = int(ts_ms)
        self.ready = bool(self.bids and self.asks)

    def top(self, levels=20):
        bids = sorted(self.bids.items(), key=lambda x: x[0], reverse=True)[: int(levels)]
        asks = sorted(self.asks.items(), key=lambda x: x[0])[: int(levels)]
        return bids, asks

    def snapshot(self, levels=20):
        bids, asks = self.top(levels)
        if not bids or not asks:
            return {"ready": False, "symbol": self.symbol}
        bid, bid_qty = bids[0]
        ask, ask_qty = asks[0]
        mid = (bid + ask) / 2.0
        spread_pct = 0.0 if bid <= 0 else (ask / bid - 1.0) * 100.0
        bid5 = sum(p * q for p, q in bids[:5])
        ask5 = sum(p * q for p, q in asks[:5])
        bid20 = sum(p * q for p, q in bids[:20])
        ask20 = sum(p * q for p, q in asks[:20])
        denom = bid_qty + ask_qty
        microprice = mid if denom <= 0 else (ask * bid_qty + bid * ask_qty) / denom
        total5 = bid5 + ask5
        imbalance5 = 0.0 if total5 <= 0 else (bid5 - ask5) / total5
        return {
            "ready": True,
            "symbol": self.symbol,
            "best_bid": bid,
            "best_ask": ask,
            "best_bid_qty": bid_qty,
            "best_ask_qty": ask_qty,
            "mid": mid,
            "spread_pct": spread_pct,
            "bid_depth_5_usdt": bid5,
            "ask_depth_5_usdt": ask5,
            "bid_depth_20_usdt": bid20,
            "ask_depth_20_usdt": ask20,
            "book_imbalance_5": imbalance5,
            "microprice": microprice,
            "microprice_edge_bps": 0.0 if mid <= 0 else (microprice / mid - 1.0) * 10000.0,
            "update_id": self.update_id,
            "seq": self.seq,
            "exchange_ts_ms": self.exchange_ts_ms,
        }
