from __future__ import annotations
import os, requests

class CoinGlassProvider:
    BASE="https://open-api-v4.coinglass.com"
    def __init__(self,api_key=None,timeout=15):
        self.api_key=api_key or os.getenv("COINGLASS_API_KEY")
        self.timeout=timeout
    @property
    def enabled(self):
        return bool(self.api_key)
    def _get(self,path,params):
        if not self.api_key:
            raise RuntimeError("COINGLASS_API_KEY is not configured")
        r=requests.get(self.BASE+path,params=params,timeout=self.timeout,headers={"accept":"application/json","CG-API-KEY":self.api_key})
        r.raise_for_status()
        p=r.json()
        if str(p.get("code"))!="0":
            raise RuntimeError(str(p))
        return p.get("data")
    def open_interest_history(self,exchange,symbol,interval="15m",limit=1000):
        return self._get("/api/futures/open-interest/history",{"exchange":exchange,"symbol":symbol,"interval":interval,"limit":limit})
    def liquidation_history(self,exchange,symbol,interval="15m",limit=1000):
        return self._get("/api/futures/liquidation/history",{"exchange":exchange,"symbol":symbol,"interval":interval,"limit":limit})
    def oi_weighted_funding_history(self,symbol,interval="30m",limit=1000):
        return self._get("/api/futures/funding-rate/oi-weight-history",{"symbol":symbol,"interval":interval,"limit":limit})
