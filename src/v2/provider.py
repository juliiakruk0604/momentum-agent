from __future__ import annotations

import time
import requests
import pandas as pd


STABLE_BASES = {
    "USDC","USDE","USDD","DAI","FDUSD","TUSD","USDP","PYUSD","USD1","RLUSD",
    "USDS","FRAX","GUSD","LUSD","USDY","USDTB","EURC"
}


class BybitV2Provider:
    BASE = "https://api.bybit.com"

    def __init__(self, timeout=12, pause=0.04, max_retries=3, backoff_base=0.35):
        self.timeout = timeout
        self.pause = pause
        self.max_retries = int(max_retries)
        self.backoff_base = float(backoff_base)

    def _get(self, path, params):
        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                r = requests.get(self.BASE + path, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                if attempt >= self.max_retries:
                    raise
                time.sleep(self.backoff_base * (2 ** attempt))
                continue
            if r.status_code == 429 or 500 <= r.status_code < 600:
                if attempt >= self.max_retries:
                    r.raise_for_status()
                time.sleep(self.backoff_base * (2 ** attempt))
                continue
            r.raise_for_status()
            payload = r.json()
            if int(payload.get("retCode", -1)) != 0:
                raise RuntimeError(f"Bybit {payload.get('retCode')}: {payload.get('retMsg')}")
            time.sleep(self.pause)
            return payload.get("result") or {}
        if last_exc:
            raise last_exc
        raise RuntimeError("Bybit request failed")

    @staticmethod
    def _frame(rows):
        if not rows:
            return pd.DataFrame(columns=["open","high","low","close","volume","turnover"])
        df = pd.DataFrame(rows, columns=["time","open","high","low","close","volume","turnover"])
        df["time"] = pd.to_datetime(df["time"].astype("int64"), unit="ms", utc=True)
        for col in ["open","high","low","close","volume","turnover"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        return df.drop_duplicates("time").sort_values("time").set_index("time")

    def kline(self, symbol, interval="15", limit=200, category="spot", start_ms=None, end_ms=None):
        params = {"category": category, "symbol": symbol, "interval": interval, "limit": int(limit)}
        if start_ms is not None:
            params["start"] = int(start_ms)
        if end_ms is not None:
            params["end"] = int(end_ms)
        return self._frame(self._get("/v5/market/kline", params).get("list") or [])

    def kline_range(self, symbol, interval, start_ms, end_ms, category="spot"):
        chunks = []
        cursor = int(end_ms)
        while cursor > int(start_ms):
            frame = self.kline(symbol, interval, 1000, category=category, start_ms=start_ms, end_ms=cursor)
            if frame.empty:
                break
            chunks.append(frame)
            oldest = int(frame.index.min().timestamp() * 1000)
            if oldest <= int(start_ms):
                break
            cursor = oldest - 1
        if not chunks:
            return self._frame([])
        out = pd.concat(chunks).sort_index()
        return out.loc[~out.index.duplicated(keep="last")]

    def tickers(self, category="spot"):
        return self._get("/v5/market/tickers", {"category": category}).get("list") or []

    def ticker(self, symbol, category="spot"):
        rows = self._get("/v5/market/tickers", {"category": category, "symbol": symbol}).get("list") or []
        if not rows:
            raise RuntimeError(f"ticker_not_found:{category}:{symbol}")
        return rows[0]

    def instruments(self, category="spot"):
        out, cursor = [], None
        while True:
            params = {"category": category, "limit": 1000}
            if cursor:
                params["cursor"] = cursor
            result = self._get("/v5/market/instruments-info", params)
            out.extend(result.get("list") or [])
            cursor = result.get("nextPageCursor")
            if not cursor:
                break
        return out

    def liquid_spot_usdt_symbols(self, limit=40, min_turnover=5_000_000):
        tradable = {
            x.get("symbol"): x
            for x in self.instruments("spot")
            if x.get("quoteCoin") == "USDT"
            and x.get("status") == "Trading"
            and str(x.get("baseCoin") or "").upper() not in STABLE_BASES
        }
        rows = []
        for t in self.tickers("spot"):
            symbol = t.get("symbol")
            if symbol not in tradable:
                continue
            turnover = float(t.get("turnover24h") or 0.0)
            if turnover < float(min_turnover):
                continue
            rows.append((symbol, turnover))
        rows.sort(key=lambda x: x[1], reverse=True)
        return [s for s, _ in rows[:int(limit)]]

    def orderbook(self, symbol, limit=25):
        result = self._get("/v5/market/orderbook", {"category": "spot", "symbol": symbol, "limit": int(limit)})
        bids = [(float(p), float(q)) for p, q in (result.get("b") or [])]
        asks = [(float(p), float(q)) for p, q in (result.get("a") or [])]
        return {"bids": bids, "asks": asks, "ts": result.get("ts")}

    def linear_oi_change_1h_pct(self, symbol):
        try:
            rows = self._get("/v5/market/open-interest", {
                "category": "linear",
                "symbol": symbol,
                "intervalTime": "15min",
                "limit": 8,
            }).get("list") or []
            if len(rows) < 5:
                return None
            points = sorted(rows, key=lambda x: int(x["timestamp"]))
            a = float(points[-5]["openInterest"])
            b = float(points[-1]["openInterest"])
            return None if a <= 0 else (b / a - 1.0) * 100.0
        except Exception:
            return None

    def linear_funding_rate(self, symbol):
        try:
            rows = self._get("/v5/market/funding/history", {
                "category": "linear", "symbol": symbol, "limit": 1
            }).get("list") or []
            return None if not rows else float(rows[0]["fundingRate"])
        except Exception:
            return None


    def recent_trades(self, symbol, limit=200, category="spot"):
        result = self._get("/v5/market/recent-trade", {
            "category": category,
            "symbol": symbol,
            "limit": int(limit),
        })
        rows = []
        for t in result.get("list") or []:
            rows.append({
                "time": int(t.get("time") or 0),
                "price": float(t.get("price") or 0.0),
                "size": float(t.get("size") or 0.0),
                "side": str(t.get("side") or ""),
            })
        return rows
