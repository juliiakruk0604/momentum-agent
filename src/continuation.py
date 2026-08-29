from __future__ import annotations
import pandas as pd
from .models import ContinuationResult

def _clip01(x):
    return max(0.0, min(1.0, float(x)))

def evaluate_continuation(signal, bars5, cfg):
    c=cfg["continuation"]
    obs=int(c["observation_minutes"])
    n=max(1,obs//5)
    x=bars5.sort_index()
    window=x[x.index>=signal.available_time].head(n)
    if len(window)<n:
        return ContinuationResult(
            signal.symbol, signal.signal_time,
            signal.available_time+pd.Timedelta(minutes=obs),
            0,0,0,0,0,False,"insufficient_5m_bars","REJECTED"
        )

    last_close=float(window.iloc[-1]["close"])
    follow=(last_close/signal.signal_price-1)*100
    ratio=float((window["close"]>signal.signal_price).mean())
    mae=(float(window["low"].min())/signal.signal_price-1)*100
    med_vol=float(window["volume"].median())
    cum_norm=float((window["volume"]/max(med_vol,1e-12)).sum())
    efficiency=max(0.0,follow)/max(cum_norm,1e-12)

    follow_s=_clip01(follow/2.0)
    persistence_s=_clip01((ratio-0.5)/0.5)
    adverse_s=_clip01((float(c["max_adverse_excursion_pct"])+mae)/float(c["max_adverse_excursion_pct"]))
    efficiency_s=_clip01(efficiency/max(float(c["min_progress_efficiency"]),1e-9))
    score=100*(0.40*follow_s+0.30*persistence_s+0.20*adverse_s+0.10*efficiency_s)

    confirmed=(
        follow>=float(c["min_followthrough_return_pct"])
        and mae>=-float(c["max_adverse_excursion_pct"])
    )
    strong=(
        confirmed
        and follow>=float(c["strong_min_followthrough_return_pct"])
        and mae>=-float(c["strong_max_adverse_excursion_pct"])
    )
    tier="STRONG" if strong else ("CONFIRMED" if confirmed else "REJECTED")

    blockers=[]
    if follow<float(c["min_followthrough_return_pct"]): blockers.append("weak_30m_followthrough")
    if mae<-float(c["max_adverse_excursion_pct"]): blockers.append("excessive_early_drawdown")
    reason=tier if confirmed else ",".join(blockers)
    return ContinuationResult(
        signal.symbol, signal.signal_time, window.index[-1]+pd.Timedelta(minutes=5),
        follow, ratio, mae, efficiency, score, confirmed, reason, tier
    )
