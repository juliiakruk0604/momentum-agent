from __future__ import annotations

from collections import deque
import math
import time


class MicrostructureFeatureEngine:
    def __init__(self, max_trade_age_seconds=30, history_seconds=30):
        self.max_trade_age_ms = int(max_trade_age_seconds * 1000)
        self.trades = {}
        self.history = {}
        self.history_seconds = int(history_seconds)

    def on_trade(self, symbol: str, trade: dict):
        dq = self.trades.setdefault(symbol, deque())
        dq.append(trade)
        cutoff = int(trade.get("time") or int(time.time() * 1000)) - self.max_trade_age_ms
        while dq and int(dq[0].get("time") or 0) < cutoff:
            dq.popleft()

    @staticmethod
    def _trade_window(rows, now_ms, seconds):
        cutoff = now_ms - int(seconds * 1000)
        selected = [t for t in rows if int(t.get("time") or 0) >= cutoff]
        buy = 0.0
        sell = 0.0
        prices = []
        for t in selected:
            p = float(t.get("price") or 0.0)
            q = float(t.get("size") or 0.0)
            notion = p * q
            prices.append(p)
            if str(t.get("side") or "").lower() == "buy":
                buy += notion
            else:
                sell += notion
        total = buy + sell
        first = prices[0] if prices else None
        last = prices[-1] if prices else None
        move_pct = 0.0 if not first or not last or first <= 0 else (last / first - 1.0) * 100.0
        return {
            "trade_count": len(selected),
            "buy_notional": buy,
            "sell_notional": sell,
            "total_notional": total,
            "buy_ratio": 0.5 if total <= 0 else buy / total,
            "cvd_ratio": 0.0 if total <= 0 else (buy - sell) / total,
            "trade_intensity_per_sec": len(selected) / max(float(seconds), 1.0),
            "price_move_pct": move_pct,
        }

    @staticmethod
    def _confidence(window):
        count_conf = min(float(window["trade_count"]) / 25.0, 1.0)
        notional_conf = min(float(window["total_notional"]) / 5000.0, 1.0)
        return math.sqrt(max(count_conf, 0.0) * max(notional_conf, 0.0))

    def compute(self, symbol: str, book_snapshot: dict, now_ms: int | None = None):
        now_ms = int(now_ms or time.time() * 1000)
        rows = list(self.trades.get(symbol) or [])
        w1 = self._trade_window(rows, now_ms, 1)
        w5 = self._trade_window(rows, now_ms, 5)

        hist = self.history.setdefault(symbol, deque())
        current = {
            "ts_ms": now_ms,
            "mid": float(book_snapshot.get("mid") or 0.0),
            "spread_pct": float(book_snapshot.get("spread_pct") or 0.0),
            "bid_depth_5_usdt": float(book_snapshot.get("bid_depth_5_usdt") or 0.0),
            "ask_depth_5_usdt": float(book_snapshot.get("ask_depth_5_usdt") or 0.0),
            "book_imbalance_5": float(book_snapshot.get("book_imbalance_5") or 0.0),
        }
        hist.append(current)
        cutoff = now_ms - self.history_seconds * 1000
        while hist and int(hist[0]["ts_ms"]) < cutoff:
            hist.popleft()

        def ago(seconds):
            target = now_ms - int(seconds * 1000)
            candidates = [x for x in hist if int(x["ts_ms"]) <= target]
            return candidates[-1] if candidates else None

        h1 = ago(1)
        h5 = ago(5)

        def pct_change(now, old):
            if old is None or float(old) <= 0:
                return 0.0
            return (float(now) / float(old) - 1.0) * 100.0

        mid = current["mid"]
        price_move_1s = pct_change(mid, None if h1 is None else h1["mid"])
        price_move_5s = pct_change(mid, None if h5 is None else h5["mid"])
        ask_depletion_1s = 0.0 if h1 is None else (
            1.0 - current["ask_depth_5_usdt"] / max(float(h1["ask_depth_5_usdt"]), 1e-9)
        )
        bid_replenishment_1s = 0.0 if h1 is None else (
            current["bid_depth_5_usdt"] / max(float(h1["bid_depth_5_usdt"]), 1e-9) - 1.0
        )
        imbalance_delta_1s = 0.0 if h1 is None else (
            current["book_imbalance_5"] - float(h1["book_imbalance_5"])
        )
        spread_delta_1s = 0.0 if h1 is None else (
            current["spread_pct"] - float(h1["spread_pct"])
        )

        conf1 = self._confidence(w1)
        conf5 = self._confidence(w5)
        micro_edge = float(book_snapshot.get("microprice_edge_bps") or 0.0)

        score = 0.0
        score += 20.0 * min(max((current["book_imbalance_5"] + 0.10) / 0.45, 0.0), 1.0)
        score += 10.0 * min(max(micro_edge, 0.0) / 2.5, 1.0)
        score += conf1 * 20.0 * min(max((w1["buy_ratio"] - 0.50) / 0.25, 0.0), 1.0)
        score += conf5 * 15.0 * min(max((w5["buy_ratio"] - 0.50) / 0.20, 0.0), 1.0)
        score += conf1 * 10.0 * min(max(w1["cvd_ratio"], 0.0) / 0.40, 1.0)
        score += 10.0 * min(max(ask_depletion_1s, 0.0) / 0.35, 1.0)
        score += 5.0 * min(max(imbalance_delta_1s, 0.0) / 0.20, 1.0)
        score += 5.0 * min(max(price_move_1s, 0.0) / 0.12, 1.0)
        score += 5.0 * min(max(price_move_5s, 0.0) / 0.30, 1.0)

        return {
            **book_snapshot,
            "ts_ms": now_ms,
            "trade_1s": w1,
            "trade_5s": w5,
            "flow_confidence_1s": conf1,
            "flow_confidence_5s": conf5,
            "price_move_1s_pct": price_move_1s,
            "price_move_5s_pct": price_move_5s,
            "ask_depletion_1s": ask_depletion_1s,
            "bid_replenishment_1s": bid_replenishment_1s,
            "book_imbalance_delta_1s": imbalance_delta_1s,
            "spread_delta_1s": spread_delta_1s,
            "microstructure_score": round(max(0.0, min(100.0, score)), 3),
        }
