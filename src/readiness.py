from .models import TradeReadiness

def derivatives_score(d,cfg):
    c=cfg["derivatives"]
    if d.oi_change_1h_pct is None:
        return None,["oi_missing"]
    blockers=[]
    oi=max(0,min(1,d.oi_change_1h_pct/10))
    funding=1.0
    if d.funding_rate is not None:
        funding=max(0,min(1,(float(c["max_abs_funding_rate"])-abs(d.funding_rate))/float(c["max_abs_funding_rate"])))
        if abs(d.funding_rate)>float(c["max_abs_funding_rate"]): blockers.append("funding_overheated")
    taker=0.5 if d.taker_buy_sell_ratio is None else max(0,min(1,(d.taker_buy_sell_ratio-0.8)/0.8))
    liq=0.5
    if d.short_liquidation_usd_1h is not None and d.long_liquidation_usd_1h is not None:
        total=d.short_liquidation_usd_1h+d.long_liquidation_usd_1h
        liq=d.short_liquidation_usd_1h/total if total>0 else 0.5
    score=100*(0.50*oi+0.20*funding+0.20*taker+0.10*liq)
    if d.oi_change_1h_pct<float(c["min_oi_change_1h_pct"]): blockers.append("oi_acceleration_weak")
    return score,blockers

def combine_readiness(impulse,cont,deriv,cfg):
    ds,blockers=derivatives_score(deriv,cfg)
    if not cont.confirmed: blockers.append("continuation_not_confirmed")
    if cfg["derivatives"]["oi_required_for_live_candidate"] and ds is None: blockers.append("live_requires_oi")
    if ds is None:
        final=0.45*impulse.impulse_score+0.55*cont.continuation_score
        state="PAPER-WATCH" if cont.confirmed else "WAIT"
    else:
        final=0.25*impulse.impulse_score+0.45*cont.continuation_score+0.30*ds
        state="EARLY ENTRY" if cont.confirmed and not blockers and final>=75 else "WAIT"
    return TradeReadiness(impulse.symbol,impulse.impulse_score,cont.continuation_score,ds,final,state,blockers)
