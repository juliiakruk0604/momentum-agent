from __future__ import annotations
import time, requests, pandas as pd
from ..models import DerivativesSnapshot

class BybitPublicProvider:
    BASE="https://api.bybit.com"
    def __init__(self,timeout=15,pause=0.08):
        self.timeout=timeout; self.pause=pause
    def _get(self,path,params):
        r=requests.get(self.BASE+path,params=params,timeout=self.timeout)
        r.raise_for_status()
        p=r.json()
        if p.get("retCode")!=0:
            raise RuntimeError(f"{p.get('retCode')}: {p.get('retMsg')}")
        time.sleep(self.pause)
        return p["result"]

    def kline(self,symbol,interval,limit=200,end_ms=None,start_ms=None):
        params={"category":"linear","symbol":symbol,"interval":interval,"limit":limit}
        if end_ms is not None: params["end"]=int(end_ms)
        if start_ms is not None: params["start"]=int(start_ms)
        rows=self._get("/v5/market/kline",params).get("list",[])
        if not rows: return pd.DataFrame(columns=["open","high","low","close","volume","turnover"])
        df=pd.DataFrame(rows,columns=["time","open","high","low","close","volume","turnover"])
        df["time"]=pd.to_datetime(df["time"].astype("int64"),unit="ms",utc=True)
        for c in ["open","high","low","close","volume","turnover"]:
            df[c]=pd.to_numeric(df[c],errors="coerce")
        return df.drop_duplicates("time").sort_values("time").set_index("time")

    def kline_range(self,symbol,interval,start_ms,end_ms):
        chunks=[]; cursor=int(end_ms)
        while cursor>int(start_ms):
            df=self.kline(symbol,interval,1000,end_ms=cursor,start_ms=start_ms)
            if df.empty: break
            chunks.append(df)
            oldest=int(df.index.min().timestamp()*1000)
            if oldest<=int(start_ms): break
            cursor=oldest-1
        if not chunks: return pd.DataFrame(columns=["open","high","low","close","volume","turnover"])
        return pd.concat(chunks).sort_index().loc[lambda x:~x.index.duplicated(keep="last")]

    def instruments(self,status=None):
        out=[]; cursor=None
        while True:
            p={"category":"linear","limit":1000}
            if status is not None: p["status"]=status
            if cursor:p["cursor"]=cursor
            res=self._get("/v5/market/instruments-info",p)
            out.extend(res.get("list",[]))
            cursor=res.get("nextPageCursor")
            if not cursor: break
        return out

    def instruments_all_statuses(self,statuses=("Trading","Settling","Closed")):
        by_symbol={}
        for status in statuses:
            try: rows=self.instruments(status=status)
            except Exception: rows=[]
            for row in rows:
                sym=row.get("symbol")
                if sym: by_symbol[sym]=row
        return list(by_symbol.values())

    def tickers(self):
        return self._get("/v5/market/tickers",{"category":"linear"}).get("list",[])

    def liquid_usdt_symbols(self,limit=150,min_turnover=1_000_000):
        instruments={x["symbol"]:x for x in self.instruments()
                     if x.get("quoteCoin")=="USDT" and x.get("status")=="Trading"
                     and x.get("contractType")=="LinearPerpetual"}
        rows=[]
        for t in self.tickers():
            s=t.get("symbol")
            if s not in instruments: continue
            turnover=float(t.get("turnover24h") or 0)
            if turnover<min_turnover: continue
            rows.append((s,turnover))
        rows.sort(key=lambda x:x[1],reverse=True)
        return [x[0] for x in rows[:limit]]

    def open_interest(self,symbol,interval="15min",limit=10,start_ms=None,end_ms=None):
        params={"category":"linear","symbol":symbol,"intervalTime":interval,"limit":limit}
        if start_ms is not None: params["startTime"]=int(start_ms)
        if end_ms is not None: params["endTime"]=int(end_ms)
        rows=self._get("/v5/market/open-interest",params).get("list",[])
        if not rows:return pd.DataFrame(columns=["open_interest"])
        df=pd.DataFrame(rows)
        df["time"]=pd.to_datetime(df["timestamp"].astype("int64"),unit="ms",utc=True)
        df["open_interest"]=pd.to_numeric(df["openInterest"],errors="coerce")
        return df[["time","open_interest"]].drop_duplicates("time").sort_values("time").set_index("time")

    def open_interest_range(self,symbol,start_ms,end_ms,interval="15min"):
        rows=[]; cursor=int(start_ms)
        step=200*15*60*1000 if interval=="15min" else 200*60*60*1000
        while cursor<int(end_ms):
            e=min(int(end_ms),cursor+step)
            df=self.open_interest(symbol,interval=interval,limit=200,start_ms=cursor,end_ms=e)
            if not df.empty: rows.append(df)
            cursor=e+1
        if not rows:return pd.DataFrame(columns=["open_interest"])
        return pd.concat(rows).sort_index().loc[lambda x:~x.index.duplicated(keep="last")]

    def funding_history(self,symbol,limit=200,start_ms=None,end_ms=None):
        params={"category":"linear","symbol":symbol,"limit":limit}
        if start_ms is not None: params["startTime"]=int(start_ms)
        if end_ms is not None: params["endTime"]=int(end_ms)
        rows=self._get("/v5/market/funding/history",params).get("list",[])
        if not rows:return pd.DataFrame(columns=["funding_rate"])
        df=pd.DataFrame(rows)
        df["time"]=pd.to_datetime(df["fundingRateTimestamp"].astype("int64"),unit="ms",utc=True)
        df["funding_rate"]=pd.to_numeric(df["fundingRate"],errors="coerce")
        return df[["time","funding_rate"]].drop_duplicates("time").sort_values("time").set_index("time")

    def funding_history_range(self,symbol,start_ms,end_ms):
        rows=[]; cursor=int(end_ms)
        while cursor>int(start_ms):
            df=self.funding_history(symbol,limit=200,start_ms=start_ms,end_ms=cursor)
            if df.empty: break
            rows.append(df)
            oldest=int(df.index.min().timestamp()*1000)
            if oldest<=int(start_ms): break
            cursor=oldest-1
        if not rows:return pd.DataFrame(columns=["funding_rate"])
        return pd.concat(rows).sort_index().loc[lambda x:~x.index.duplicated(keep="last")]

    def funding(self,symbol,limit=3):
        df=self.funding_history(symbol,limit=limit)
        return None if df.empty else df.iloc[-1]

    def derivatives_snapshot(self,symbol):
        oi=self.open_interest(symbol,limit=8)
        oi_change=None
        if len(oi)>=5:
            a=float(oi.iloc[-5]["open_interest"]); b=float(oi.iloc[-1]["open_interest"])
            if a>0: oi_change=(b/a-1)*100
        f=self.funding(symbol)
        funding_rate=None if f is None else float(f["funding_rate"])
        return DerivativesSnapshot(oi_change_1h_pct=oi_change,funding_rate=funding_rate,source="BYBIT")
