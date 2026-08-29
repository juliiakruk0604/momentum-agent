from pathlib import Path
import pandas as pd
import yaml
from src.models import ImpulseSignal, DerivativesSnapshot
from src.continuation import evaluate_continuation
from src.readiness import combine_readiness
from src.labeling import label_future_moves

ROOT=Path(__file__).resolve().parents[1]
CFG=yaml.safe_load((ROOT/"config.yaml").read_text())

def load(sym):
    x=pd.read_csv(ROOT/"tests/fixtures"/f"{sym}_5m.csv",parse_dates=["time"]).set_index("time")
    x.index=pd.to_datetime(x.index,utc=True)
    return x

def sig(sym,signal_time,price,impulse=85):
    t=pd.Timestamp(signal_time)
    if t.tzinfo is None: t=t.tz_localize("UTC")
    return ImpulseSignal(sym,t,t+pd.Timedelta(minutes=15),price,price/1.03,3,0.8,10,1.5,impulse)

def test_real_dexe_continuation_confirms():
    r=evaluate_continuation(sig("DEXEUSDT","2026-08-28T11:00:00Z",1.945),load("DEXEUSDT"),CFG)
    assert r.confirmed and r.followthrough_return_pct > 2.5

def test_real_profitable_sui_continuation_confirms():
    r=evaluate_continuation(sig("SUIUSDT","2026-08-20T08:30:00Z",0.7221),load("SUIUSDT"),CFG)
    assert r.confirmed and r.followthrough_return_pct > 0.5

def test_real_sol_false_impulse_rejected():
    r=evaluate_continuation(sig("SOLUSDT","2026-08-23T00:30:00Z",96.33),load("SOLUSDT"),CFG)
    assert not r.confirmed

def test_real_doge_false_impulse_rejected():
    r=evaluate_continuation(sig("DOGEUSDT","2026-08-25T00:30:00Z",0.09115),load("DOGEUSDT"),CFG)
    assert not r.confirmed

def test_missing_oi_never_live():
    s=sig("DEXEUSDT","2026-08-28T11:00:00Z",1.945)
    c=evaluate_continuation(s,load("DEXEUSDT"),CFG)
    r=combine_readiness(s,c,DerivativesSnapshot(),CFG)
    assert r.state=="PAPER-WATCH" and "live_requires_oi" in r.blockers

def test_good_derivatives_promote():
    s=sig("DEXEUSDT","2026-08-28T11:00:00Z",1.945,95)
    c=evaluate_continuation(s,load("DEXEUSDT"),CFG)
    d=DerivativesSnapshot(oi_change_1h_pct=8.0,funding_rate=0.0001,taker_buy_sell_ratio=1.4,
                          short_liquidation_usd_1h=1_000_000,long_liquidation_usd_1h=250_000,source="TEST")
    assert combine_readiness(s,c,d,CFG).state=="EARLY ENTRY"

def test_future_label_stops_after_invalidation():
    s=sig("TEST","2026-01-01T00:00:00Z",100)
    idx=pd.date_range(s.available_time,periods=120,freq="1min",tz="UTC")
    x=pd.DataFrame({"open":100.0,"high":100.5,"low":99.5,"close":100.0,"volume":1.0},index=idx)
    x.iloc[5,x.columns.get_loc("low")]=95.0
    x.iloc[50,x.columns.get_loc("high")]=140.0
    labels=label_future_moves(s,x,CFG)
    assert not labels[-1].hit_20_before_invalidation and labels[-1].invalidated

def test_vet_runner_case_confirms_strong():
    r=evaluate_continuation(sig("VETUSDT","2026-08-26T16:15:00Z",0.005726),load("VETUSDT"),CFG)
    assert r.confirmed and r.tier=="STRONG" and r.followthrough_return_pct > 1.5

def test_pyth_confirms_without_score_veto():
    r=evaluate_continuation(sig("PYTHUSDT","2026-08-25T00:00:00Z",0.05014),load("PYTHUSDT"),CFG)
    assert r.confirmed and r.followthrough_return_pct > 0.9

def test_pendle_confirms():
    r=evaluate_continuation(sig("PENDLEUSDT","2026-08-27T08:00:00Z",1.771),load("PENDLEUSDT"),CFG)
    assert r.confirmed and r.followthrough_return_pct > 0.8

def test_future_label_uses_wall_clock():
    s=sig("TEST","2026-01-01T00:00:00Z",100)
    idx=pd.date_range(s.available_time,periods=20,freq="15min",tz="UTC")
    x=pd.DataFrame({"open":100.0,"high":101.0,"low":99.5,"close":100.5,"volume":1.0},index=idx)
    one_hour=next(z for z in label_future_moves(s,x,CFG) if z.horizon_minutes==60)
    assert one_hour.time_to_mfe_minutes <= 45
    assert abs(one_hour.close_return_pct-0.5) < 1e-9

def test_sqlite_store_runtime_and_labels(tmp_path):
    from src.store import SignalStore
    p=tmp_path/"test.db"
    s=SignalStore(path=str(p),database_url=None)
    t=pd.Timestamp("2026-01-01T00:00:00Z")
    imp=ImpulseSignal("TESTUSDT",t,t+pd.Timedelta(minutes=15),1.0,0.97,3.0,0.5,7.0,1.2,80.0)
    s.upsert_impulse(imp)
    s.set_runtime("heartbeat",{"ok":True})
    assert s.get_runtime("heartbeat")["value"]["ok"] is True


def test_research_status_tracks_oi_and_24h_labels(tmp_path):
    from src.store import SignalStore
    from src.models import ContinuationResult, TradeReadiness
    p=tmp_path/"research.db"
    store=SignalStore(path=str(p),database_url=None)
    t=pd.Timestamp("2026-01-01T00:00:00Z")
    imp=ImpulseSignal("EDGEUSDT",t,t+pd.Timedelta(minutes=15),100.0,97.0,3.0,0.8,10.0,1.5,90.0)
    store.upsert_impulse(imp)
    cont=ContinuationResult("EDGEUSDT",t,t+pd.Timedelta(minutes=45),1.2,0.8,-0.4,0.2,60.0,True,"STRONG","STRONG")
    deriv=DerivativesSnapshot(oi_change_1h_pct=4.0,funding_rate=0.0001,source="TEST")
    ready=TradeReadiness("EDGEUSDT",90.0,60.0,70.0,75.0,"EARLY ENTRY",[])
    store.finalize("EDGEUSDT",t,cont,ready,deriv)
    labels=[{
        "symbol":"EDGEUSDT","signal_time":str(t),"horizon_minutes":1440,
        "mfe_pct":12.0,"mae_pct":-1.0,"close_return_pct":8.0,
        "hit_5_before_invalidation":True,"hit_10_before_invalidation":True,
        "hit_20_before_invalidation":False,"hit_30_before_invalidation":False,
        "time_to_mfe_minutes":300,"invalidated":False,
    }]
    store.update_labels("EDGEUSDT",t,labels,1440)
    status=store.research_status()
    assert status["dataset"]["confirmed"]==1
    assert status["dataset"]["confirmed_oi_up"]==1
    assert status["cohorts"]["continuation_plus_oi_up"]["p_hit_10"]==1.0
    assert status["research_gate"]["passed"] is False


def test_daily_snapshot_roundtrip(tmp_path):
    from src.store import SignalStore
    store=SignalStore(path=str(tmp_path/"snap.db"),database_url=None)
    payload={"dataset":{"total_impulses":3},"research_gate":{"passed":False,"reasons":["small"]}}
    store.save_daily_snapshot(payload,"2026-08-29")
    rows=store.snapshots()
    assert rows[0]["snapshot_date"]=="2026-08-29"
    assert rows[0]["payload"]["dataset"]["total_impulses"]==3


def test_worker_health_fresh_heartbeat(tmp_path):
    from src.store import SignalStore
    store=SignalStore(path=str(tmp_path/"health.db"),database_url=None)
    store.set_runtime("worker_heartbeat",{"symbols":150,"scan_errors":0,"continuation_errors":0,"label_errors":0})
    h=store.worker_health(300)
    assert h["status"]=="healthy"
    assert h["stale"] is False


def test_market_scan_bucket_only_once_per_15m(tmp_path):
    from src.store import SignalStore
    from worker import _should_scan_market, _market_bucket
    store=SignalStore(path=str(tmp_path/"bucket.db"),database_url=None)
    now=pd.Timestamp("2026-08-29T06:07:00Z")
    assert _should_scan_market(store,now) is True
    store.set_runtime("market_scan_bucket",_market_bucket(now))
    assert _should_scan_market(store,pd.Timestamp("2026-08-29T06:14:59Z")) is False
    assert _should_scan_market(store,pd.Timestamp("2026-08-29T06:15:01Z")) is True


def test_confirmed_candidates_filters_rejected(tmp_path):
    from src.store import SignalStore
    from src.models import ContinuationResult, TradeReadiness
    store=SignalStore(path=str(tmp_path/"cand.db"),database_url=None)
    t=pd.Timestamp("2026-01-01T00:00:00Z")
    for i,confirmed in enumerate([False,True]):
        tt=t+pd.Timedelta(hours=i)
        imp=ImpulseSignal(f"X{i}USDT",tt,tt+pd.Timedelta(minutes=15),100,97,3,0.8,10,1.5,85)
        store.upsert_impulse(imp)
        cont=ContinuationResult(imp.symbol,tt,tt+pd.Timedelta(minutes=45),1 if confirmed else -1,0.5,-0.5,0.1,50,confirmed,"CONFIRMED" if confirmed else "weak","CONFIRMED" if confirmed else "REJECTED")
        ready=TradeReadiness(imp.symbol,85,50,None,60,"PAPER-WATCH" if confirmed else "WAIT",[])
        store.finalize(imp.symbol,tt,cont,ready,DerivativesSnapshot())
    rows=store.confirmed_candidates()
    assert len(rows)==1
    assert rows[0]["symbol"]=="X1USDT"


def test_historical_derivatives_respects_publication_lag():
    from src.historical_backfill import historical_derivatives
    idx=pd.date_range("2026-01-01T00:00:00Z",periods=7,freq="15min",tz="UTC")
    oi=pd.DataFrame({"open_interest":[100,101,102,104,106,108,120]},index=idx)
    funding=pd.DataFrame({"funding_rate":[0.0001]},index=[idx[0]])
    d=historical_derivatives(oi,funding,idx[-1],publication_lag_bars=1)
    # With one publication-lag bar, 120 is unavailable; compare 108 vs 101.
    assert round(d.oi_change_1h_pct,6)==round((108/101-1)*100,6)
    assert d.funding_rate==0.0001


def test_active_overlap_point_in_time():
    from src.historical_backfill import active_overlap
    start=pd.Timestamp("2026-06-01T00:00:00Z")
    end=pd.Timestamp("2026-07-01T00:00:00Z")
    active={"launchTime":int(pd.Timestamp("2026-05-01T00:00:00Z").timestamp()*1000),"deliveryTime":0}
    future={"launchTime":int(pd.Timestamp("2026-08-01T00:00:00Z").timestamp()*1000),"deliveryTime":0}
    ended={"launchTime":int(pd.Timestamp("2026-01-01T00:00:00Z").timestamp()*1000),"deliveryTime":int(pd.Timestamp("2026-05-01T00:00:00Z").timestamp()*1000)}
    assert active_overlap(active,start,end) is True
    assert active_overlap(future,start,end) is False
    assert active_overlap(ended,start,end) is False


def test_historical_store_and_oos_status(tmp_path):
    from src.store import SignalStore
    from src.models import ContinuationResult
    store=SignalStore(path=str(tmp_path/"hist.db"),database_url=None)
    t=pd.Timestamp("2026-06-25T00:00:00Z")
    imp=ImpulseSignal("HISTUSDT",t,t+pd.Timedelta(minutes=15),100,97,3,0.8,10,1.5,88)
    cont=ContinuationResult("HISTUSDT",t,t+pd.Timedelta(minutes=45),1.2,0.7,-0.5,0.1,55,True,"CONFIRMED","CONFIRMED")
    deriv=DerivativesSnapshot(oi_change_1h_pct=3.0,funding_rate=0.0001,source="BYBIT_HISTORICAL")
    labels=[{
        "symbol":"HISTUSDT","signal_time":str(t),"horizon_minutes":1440,
        "mfe_pct":11.0,"mae_pct":-1.2,"close_return_pct":7.0,
        "hit_5_before_invalidation":True,"hit_10_before_invalidation":True,
        "hit_20_before_invalidation":False,"hit_30_before_invalidation":False,
        "time_to_mfe_minutes":420,"invalidated":False,
    }]
    state={
        "dataset_id":"ds","start":"2026-06-01T00:00:00+00:00","end":"2026-07-01T00:00:00+00:00",
        "cursor":1,"universe":[{"symbol":"HISTUSDT"}],"complete":True,
        "closed_or_delivered_contracts":0,"survivorship_warning":True,
    }
    store.set_runtime("historical_backfill_state",state)
    store.upsert_historical_event("ds",imp,cont,deriv,labels,0)
    store.record_historical_symbol_run("ds","HISTUSDT","ok",1000,1,100,10,None)
    status=store.historical_status()
    assert status["status"]=="complete"
    assert status["oos"]["confirmed_oi_up"]==1
    assert status["oos"]["cohorts"]["continuation_plus_oi_up_oos"]["p_hit_10"]==1.0


def test_micro_live_readiness_stays_false_on_small_sample(tmp_path):
    from src.store import SignalStore
    store=SignalStore(path=str(tmp_path/"micro.db"),database_url=None)
    r=store.micro_live_readiness()
    assert r["ready"] is False
    assert r["provisional_only"] is True
    assert "historical_oos_not_available" in r["reasons"]


def test_execution_preflight_respects_risk_budget():
    from src.execution.preflight import build_execution_plan
    limits={
        "hard_stop_pct":4.0,
        "target_risk_fraction_of_equity":0.01,
        "max_notional_fraction_of_equity":0.25,
        "max_daily_loss_usdt":0.25,
        "max_leverage":1,
    }
    p=build_execution_plan(symbol="TESTUSDT",entry_price=2.0,equity_usdt=100.0,risk_limits=limits)
    assert p.allowed is True
    assert abs(p.notional_usdt-25.0)<1e-9
    assert abs(p.estimated_loss_at_stop_usdt-1.0)<1e-9


def test_execution_preflight_blocks_exchange_minimum():
    from src.execution.preflight import build_execution_plan
    limits={
        "hard_stop_pct":4.0,
        "target_risk_fraction_of_equity":0.01,
        "max_notional_fraction_of_equity":0.25,
        "max_daily_loss_usdt":0.25,
        "max_leverage":1,
    }
    p=build_execution_plan(symbol="TESTUSDT",entry_price=2.0,equity_usdt=10.0,risk_limits=limits,min_notional_usdt=5.0)
    assert p.allowed is False
    assert "exchange_min_order_exceeds_risk_budget" in p.blockers
