from __future__ import annotations
import pandas as pd
from .models import ImpulseSignal

def _clip01(x):
    return max(0.0, min(1.0, float(x)))

def compute_impulse_candidates(symbol, bars15, btc15, eth15, cfg):
    c = cfg["impulse"]
    x = bars15.sort_index()
    b = btc15["close"].reindex(x.index, method="ffill")
    e = eth15["close"].reindex(x.index, method="ffill")
    out, last_i = [], -10000
    min_i=max(c["base_window_bars_15m"],c["volume_window_bars_15m"],c["rs_window_bars_15m"])
    for i in range(int(min_i), len(x)-1):
        row=x.iloc[i]
        prev_base=x.iloc[i-int(c["base_window_bars_15m"]):i]
        prev_vol=x.iloc[i-int(c["volume_window_bars_15m"]):i]
        base=float(prev_base["low"].min())
        prev_high=float(prev_vol["high"].max())
        vol_med=float(prev_vol["volume"].median())
        if base<=0 or prev_high<=0 or vol_med<=0: continue
        move=(float(row["close"])/base-1)*100
        breakout=(float(row["close"])/prev_high-1)*100
        vr=float(row["volume"])/vol_med
        lb=int(c["rs_window_bars_15m"])
        asset_ret=float(row["close"])/float(x.iloc[i-lb]["close"])-1
        btc_ret=float(b.iloc[i])/float(b.iloc[i-lb])-1
        eth_ret=float(e.iloc[i])/float(e.iloc[i-lb])-1
        rs_pct=(asset_ret-(btc_ret+eth_ret)/2)*100
        ok=(0<=move<=float(c["max_move_from_base_pct"])
            and breakout>=float(c["min_breakout_pct"])
            and vr>=float(c["min_volume_ratio"])
            and rs_pct>=float(c["min_relative_strength_pct"]))
        if not ok: continue
        if i-last_i<int(c["cooldown_bars_15m"]): continue
        last_i=i
        move_score=_clip01((float(c["max_move_from_base_pct"])-move)/float(c["max_move_from_base_pct"]))
        breakout_score=_clip01(breakout/2.0)
        volume_score=_clip01((vr-float(c["min_volume_ratio"]))/15.0+0.5)
        rs_score=_clip01(rs_pct/3.0)
        score=100*(0.15*move_score+0.25*breakout_score+0.35*volume_score+0.25*rs_score)
        ts=x.index[i]
        out.append(ImpulseSignal(symbol,ts,ts+pd.Timedelta(minutes=15),float(row["close"]),base,move,breakout,vr,rs_pct,score))
    return out
