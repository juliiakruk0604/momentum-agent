from __future__ import annotations
import argparse, json
from dataclasses import asdict
from pathlib import Path
import pandas as pd, yaml
from src.providers.bybit_public import BybitPublicProvider
from src.impulse import compute_impulse_candidates
from src.continuation import evaluate_continuation
from src.labeling import label_future_moves

def ms(x): return int(pd.Timestamp(x).timestamp()*1000)

def load_cfg(path="config.yaml"):
    return yaml.safe_load(Path(path).read_text())

def active_overlap(meta,start,end):
    launch=pd.to_datetime(int(meta.get("launchTime") or 0),unit="ms",utc=True)
    delivery_n=int(meta.get("deliveryTime") or 0)
    delivery=None if delivery_n<=0 else pd.to_datetime(delivery_n,unit="ms",utc=True)
    return launch<=end and (delivery is None or delivery>start)

def build_folds(start,end,cfg):
    wf=cfg["walk_forward"]
    train=pd.Timedelta(days=int(wf["train_days"]))
    test=pd.Timedelta(days=int(wf["test_days"]))
    step=pd.Timedelta(days=int(wf["step_days"]))
    folds=[]; test_start=start+train; i=0
    while test_start<end:
        test_end=min(test_start+test,end)
        folds.append({"fold_id":i,"train_start":test_start-train,"train_end":test_start,"test_start":test_start,"test_end":test_end})
        i+=1; test_start+=step
    return folds

def fold_for(ts,folds):
    for f in folds:
        if f["test_start"]<=ts<f["test_end"]: return f["fold_id"]
    return None

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--days",type=int,default=30)
    ap.add_argument("--max-symbols",type=int,default=100)
    ap.add_argument("--out",default="results/research_latest")
    args=ap.parse_args()
    cfg=load_cfg(); p=BybitPublicProvider()
    end=pd.Timestamp.now(tz="UTC").floor("15min")
    start=end-pd.Timedelta(days=args.days)
    folds=build_folds(start,end,cfg)
    out=Path(args.out); out.mkdir(parents=True,exist_ok=True)
    pd.DataFrame(folds).to_csv(out/"walk_forward_folds.csv",index=False)

    raw=p.instruments_all_statuses(cfg["research"]["statuses"])
    universe=[m for m in raw if m.get("quoteCoin")=="USDT" and m.get("contractType")=="LinearPerpetual" and active_overlap(m,start,end)]
    universe=sorted(universe,key=lambda m:(int(m.get("launchTime") or 0),m.get("symbol","")))
    if args.max_symbols: universe=universe[:args.max_symbols]

    btc=p.kline_range("BTCUSDT","15",ms(start),ms(end))
    eth=p.kline_range("ETHUSDT","15",ms(start),ms(end))
    records=[]; quality=[]

    for n,meta in enumerate(universe,1):
        symbol=meta["symbol"]
        try:
            bars15=p.kline_range(symbol,"15",ms(start),ms(end))
            impulses=compute_impulse_candidates(symbol,bars15,btc,eth,cfg)
            oi=p.open_interest_range(symbol,ms(start),ms(end),"15min")
            funding=p.funding_history_range(symbol,ms(start),ms(end))
            for imp in impulses:
                cont_end=imp.available_time+pd.Timedelta(minutes=cfg["continuation"]["observation_minutes"]+10)
                bars5=p.kline_range(symbol,"5",ms(imp.available_time),ms(cont_end))
                cont=evaluate_continuation(imp,bars5,cfg)
                future_end=imp.available_time+pd.Timedelta(minutes=max(cfg["labeling"]["horizons_minutes"])+15)
                future15=p.kline_range(symbol,"15",ms(imp.available_time),ms(future_end))
                labels=label_future_moves(imp,future15,cfg)

                oi_hist=oi[oi.index<=imp.available_time].copy()
                lag=int(cfg["research"].get("publication_lag_bars",1))
                oi_change=None
                if len(oi_hist)>=5+lag:
                    use=oi_hist.iloc[:-lag] if lag else oi_hist
                    if len(use)>=5 and float(use.iloc[-5]["open_interest"])>0:
                        oi_change=(float(use.iloc[-1]["open_interest"])/float(use.iloc[-5]["open_interest"])-1)*100
                f_hist=funding[funding.index<=imp.available_time]
                f_rate=None if f_hist.empty else float(f_hist.iloc[-1]["funding_rate"])
                lab24=next((x for x in labels if x.horizon_minutes==1440),None)
                records.append({
                    **asdict(imp),
                    "continuation_tier":cont.tier,
                    "continuation_confirmed":cont.confirmed,
                    "followthrough_30m_pct":cont.followthrough_return_pct,
                    "continuation_mae_30m_pct":cont.mae_pct,
                    "continuation_score_diagnostic":cont.continuation_score,
                    "oi_change_1h_pct":oi_change,
                    "funding_rate":f_rate,
                    "fold_id":fold_for(imp.available_time,folds),
                    "mfe_24h_pct":None if lab24 is None else lab24.mfe_pct,
                    "mae_24h_pct":None if lab24 is None else lab24.mae_pct,
                    "hit_5":None if lab24 is None else lab24.hit_5_before_invalidation,
                    "hit_10":None if lab24 is None else lab24.hit_10_before_invalidation,
                    "hit_20":None if lab24 is None else lab24.hit_20_before_invalidation,
                    "hit_30":None if lab24 is None else lab24.hit_30_before_invalidation,
                    "invalidated":None if lab24 is None else lab24.invalidated,
                })
            quality.append({"symbol":symbol,"status":"ok","bars15":len(bars15),"impulses":len(impulses),"oi_rows":len(oi),"funding_rows":len(funding)})
            print(f"[{n}/{len(universe)}] {symbol}: {len(impulses)} impulses")
        except Exception as exc:
            quality.append({"symbol":symbol,"status":"error","error":repr(exc)})
            print(f"[{n}/{len(universe)}] {symbol}: ERROR {exc}")

    df=pd.DataFrame(records)
    q=pd.DataFrame(quality)
    df.to_csv(out/"signals.csv",index=False); q.to_csv(out/"data_quality.csv",index=False)
    closed_count=sum(1 for m in universe if str(m.get("status"))=="Closed" or int(m.get("deliveryTime") or 0)>0)
    summary={
        "symbols":len(universe),
        "closed_or_delivered_contracts":closed_count,
        "survivorship_warning": closed_count==0,
        "signals":len(df),
        "data_errors":int((q.status=="error").sum()) if len(q) else 0,
        "folds":len(folds),
    }
    if len(df):
        complete=df.dropna(subset=["mfe_24h_pct"])
        oos=complete[complete.fold_id.notna()].copy()
        summary["complete_24h_signals"]=len(complete)
        summary["oos_24h_signals"]=len(oos)
        summary["cohorts"]={}
        cohorts={
            "impulse_all_oos":oos,
            "continuation_confirmed_oos":oos[oos.continuation_confirmed==True],
            "continuation_strong_oos":oos[oos.continuation_tier=="STRONG"],
            "continuation_plus_oi_up_oos":oos[(oos.continuation_confirmed==True)&(oos.oi_change_1h_pct>0)],
            "continuation_plus_oi_2pct_oos":oos[(oos.continuation_confirmed==True)&(oos.oi_change_1h_pct>=2)],
        }
        for name,x in cohorts.items():
            summary["cohorts"][name]={
                "n":len(x),
                "p5":None if len(x)==0 else float(x.hit_5.mean()),
                "p10":None if len(x)==0 else float(x.hit_10.mean()),
                "p20":None if len(x)==0 else float(x.hit_20.mean()),
                "p30":None if len(x)==0 else float(x.hit_30.mean()),
                "median_mfe":None if len(x)==0 else float(x.mfe_24h_pct.median()),
                "median_mae":None if len(x)==0 else float(x.mae_24h_pct.median()),
                "invalidation_rate":None if len(x)==0 else float(x.invalidated.mean()),
            }
        fold_rows=[]
        for fid,g in oos.groupby("fold_id"):
            c=g[g.continuation_confirmed==True]
            fold_rows.append({
                "fold_id":int(fid),"impulses":len(g),"confirmed":len(c),
                "impulse_p10":None if len(g)==0 else float(g.hit_10.mean()),
                "confirmed_p10":None if len(c)==0 else float(c.hit_10.mean()),
                "confirmed_median_mfe":None if len(c)==0 else float(c.mfe_24h_pct.median()),
                "confirmed_invalidation_rate":None if len(c)==0 else float(c.invalidated.mean()),
            })
        pd.DataFrame(fold_rows).to_csv(out/"metrics_by_fold.csv",index=False)
        imp=summary["cohorts"]["impulse_all_oos"]
        con=summary["cohorts"]["continuation_confirmed_oos"]
        oi=summary["cohorts"]["continuation_plus_oi_up_oos"]
        gate_reasons=[]
        if imp["n"]<100: gate_reasons.append("need_at_least_100_oos_impulses")
        if con["n"]<30: gate_reasons.append("need_at_least_30_oos_confirmed")
        if oi["n"]<20: gate_reasons.append("need_at_least_20_oos_oi_confirmed")
        if imp["n"] and con["n"] and (con["p10"] or 0)<=(imp["p10"] or 0): gate_reasons.append("continuation_does_not_improve_p10")
        if con["n"] and con["invalidation_rate"] is not None and con["invalidation_rate"]>0.40: gate_reasons.append("confirmed_invalidation_too_high")
        summary["research_gate"]={"passed":len(gate_reasons)==0,"reasons":gate_reasons}
    (out/"summary.json").write_text(json.dumps(summary,indent=2,default=str))
    print(json.dumps(summary,indent=2))

if __name__=="__main__": main()
