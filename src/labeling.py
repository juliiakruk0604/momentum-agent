from __future__ import annotations
import pandas as pd
from .models import FutureMoveLabel

def label_future_moves(signal,bars,cfg):
    c=cfg["labeling"]
    invalidation=-float(c["invalidation_pct"])
    x=bars.sort_index()
    x=x[x.index>=signal.available_time]
    out=[]
    for horizon in c["horizons_minutes"]:
        h=int(horizon)
        end=signal.available_time+pd.Timedelta(minutes=h)
        ww=x[(x.index>=signal.available_time)&(x.index<end)]
        if ww.empty: continue
        mfe=-1e9; mae=1e9; t_mfe=0; invalidated=False
        hit={5:False,10:False,20:False,30:False}
        used_rows=[]
        for ts,b in ww.iterrows():
            used_rows.append((ts,b))
            lo=(float(b["low"])/signal.signal_price-1)*100
            hi=(float(b["high"])/signal.signal_price-1)*100
            mae=min(mae,lo)
            if lo<=invalidation:
                invalidated=True
                break
            if hi>mfe:
                mfe=hi
                t_mfe=max(0,int((ts-signal.available_time).total_seconds()//60))
            for t in hit:
                if hi>=t: hit[t]=True
        last_ts,last_b=used_rows[-1]
        close_ret=(float(last_b["close"])/signal.signal_price-1)*100
        out.append(FutureMoveLabel(
            signal.symbol,signal.signal_time,h,max(float(mfe),0.0),
            float(mae if mae<1e8 else 0),float(close_ret),
            hit[5],hit[10],hit[20],hit[30],int(t_mfe),invalidated
        ))
    return out
