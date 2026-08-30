from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import yaml

from src.continuation import evaluate_continuation
from src.historical_backfill import HistoricalBackfillRunner
from src.impulse import compute_impulse_candidates
from src.labeling import label_future_moves
from src.models import ImpulseSignal
from src.providers.bybit_public import BybitPublicProvider
from src.readiness import combine_readiness
from src.promo_scanner import scan_promos
from src.bybit_account import account_diagnostic, funding_balances
from src.store import SignalStore


def load_cfg():
    return yaml.safe_load(Path("config.yaml").read_text())


def decode_impulse(row):
    p = json.loads(row["impulse_json"])
    p["signal_time"] = pd.Timestamp(p["signal_time"])
    p["available_time"] = pd.Timestamp(p["available_time"])
    return ImpulseSignal(**p)


def _ms(ts):
    return int(pd.Timestamp(ts).timestamp() * 1000)


def _store_impulse_if_new(store, impulse):
    for row in store.recent(limit=250):
        if row.get("symbol") != impulse.symbol:
            continue
        if pd.Timestamp(row.get("signal_time")) == pd.Timestamp(impulse.signal_time):
            return False
    store.upsert_impulse(impulse)
    return True


def process_labels(provider, store, cfg, now):
    horizons = sorted(int(x) for x in cfg["labeling"]["horizons_minutes"])
    done = 0
    errors = 0
    for row in store.labeling_candidates(limit=250):
        imp = decode_impulse(row)
        elapsed = int((now - imp.available_time).total_seconds() // 60)
        due = [h for h in horizons if h <= elapsed]
        if not due:
            continue
        max_due = max(due)
        if int(row.get("last_labeled_horizon") or 0) >= max_due:
            continue
        try:
            end = imp.available_time + pd.Timedelta(minutes=max_due + 30)
            bars = provider.kline_range(imp.symbol, "15", _ms(imp.available_time), _ms(end))
            labels = label_future_moves(imp, bars, cfg)
            serial = [asdict(x) for x in labels if x.horizon_minutes <= max_due]
            store.update_labels(imp.symbol, imp.signal_time, serial, max_due)
            done += 1
        except Exception as exc:
            errors += 1
            print("label_error", imp.symbol, repr(exc), flush=True)
    return {"labeled": done, "label_errors": errors}


def _market_bucket(now):
    return pd.Timestamp(now).floor("15min").isoformat()


def _should_scan_market(store, now):
    bucket = _market_bucket(now)
    last = store.get_runtime("market_scan_bucket")
    return last is None or last.get("value") != bucket


def _prepare_provenance_rebuild(backfill, store):
    state = backfill.ensure_state()
    if not state.get("generator_provenance_missing"):
        return None

    universe = state.get("universe") or []
    cursor = int(state.get("cursor") or 0)
    if cursor < len(universe):
        return None

    old_dataset_id = str(state.get("dataset_id") or "unknown")
    archive_key = f"historical_backfill_superseded:{old_dataset_id}"
    if store.get_runtime(archive_key) is None:
        archived = {
            **state,
            "superseded_at": str(pd.Timestamp.now(tz="UTC")),
            "superseded_reason": "generator_provenance_missing",
        }
        store.set_runtime(archive_key, archived)

    new_state = backfill._new_state()
    return {
        "reason": "generator_provenance_missing",
        "from_dataset_id": old_dataset_id,
        "to_dataset_id": new_state.get("dataset_id"),
    }



def _promo_bucket(now):
    return pd.Timestamp(now).floor("15min").isoformat()


def process_promo_scan(store, now):
    bucket = _promo_bucket(now)
    last = store.get_runtime("promo_scan_bucket")
    if last is not None and last.get("value") == bucket:
        return {"performed": False, "bucket": bucket}

    result = scan_promos(limit=50)
    store.set_runtime("promo_scan", result)
    store.set_runtime("promo_scan_bucket", bucket)

    account = account_diagnostic()
    funding = funding_balances()
    private_snapshot = {
        "checked_at": str(pd.Timestamp.now(tz="UTC")),
        "connected": account.get("connected"),
        "spot_trade_enabled": account.get("spot_trade_enabled"),
        "forbidden_permissions": account.get("forbidden_permissions"),
        "unified_total_equity_usd": account.get("total_equity_usd"),
        "funding": funding,
        "execution_enabled": False,
    }
    store.set_runtime("promo_account_snapshot", private_snapshot)
    print("PROMO_ACCOUNT", json.dumps(private_snapshot, default=str), flush=True)

    top = (result.get("candidates") or [])[:5]
    print("PROMO_SCAN", json.dumps({
        "scanned": result.get("scanned"),
        "top": top,
        "execution_enabled": False,
    }, default=str), flush=True)
    return {
        "performed": True,
        "bucket": bucket,
        "scanned": result.get("scanned"),
        "top_count": len(top),
    }

def run_once(provider, store, cfg, universe_limit=100):
    started = pd.Timestamp.now(tz="UTC")
    now = started
    bucket = _market_bucket(now)
    scan_performed = _should_scan_market(store, now)
    symbols = []
    new_count = 0
    scan_errors = 0

    if scan_performed:
        btc = provider.kline("BTCUSDT", "15", 200)
        eth = provider.kline("ETHUSDT", "15", 200)
        symbols = provider.liquid_usdt_symbols(limit=universe_limit)
        failed_symbols = []
        for symbol in symbols:
            try:
                bars = provider.kline(symbol, "15", 200)
                impulses = compute_impulse_candidates(symbol, bars, btc, eth, cfg)
                if impulses:
                    newest = impulses[-1]
                    if pd.Timestamp.now(tz="UTC") - newest.available_time <= pd.Timedelta(minutes=45):
                        if _store_impulse_if_new(store, newest):
                            new_count += 1
            except Exception as exc:
                failed_symbols.append(symbol)
                print("scan_error", symbol, repr(exc), flush=True)

        retry_failures = []
        for symbol in failed_symbols:
            try:
                bars = provider.kline(symbol, "15", 200)
                impulses = compute_impulse_candidates(symbol, bars, btc, eth, cfg)
                if impulses:
                    newest = impulses[-1]
                    if pd.Timestamp.now(tz="UTC") - newest.available_time <= pd.Timedelta(minutes=45):
                        if _store_impulse_if_new(store, newest):
                            new_count += 1
                print("scan_retry_recovered", symbol, flush=True)
            except Exception as exc:
                retry_failures.append(symbol)
                print("scan_retry_error", symbol, repr(exc), flush=True)

        scan_errors = len(retry_failures)
        store.set_runtime("market_scan_bucket", bucket)
        store.set_runtime("market_scan_summary", {
            "bucket": bucket,
            "symbols": len(symbols),
            "new_impulses": new_count,
            "initial_scan_errors": len(failed_symbols),
            "scan_errors": scan_errors,
            "retry_recovered": len(failed_symbols) - scan_errors,
            "failed_symbols": retry_failures,
            "finished_at": str(pd.Timestamp.now(tz="UTC")),
        })

    previous_scan = store.get_runtime("market_scan_summary")
    last_market_symbols = int(((previous_scan or {}).get("value") or {}).get("symbols") or len(symbols))

    processed = 0
    continuation_errors = 0
    now = pd.Timestamp.now(tz="UTC")
    for row in store.pending():
        imp = decode_impulse(row)
        due = imp.available_time + pd.Timedelta(minutes=int(cfg["continuation"]["observation_minutes"]))
        if now < due:
            continue
        try:
            start = imp.available_time
            end = due + pd.Timedelta(minutes=10)
            bars5 = provider.kline_range(imp.symbol, "5", _ms(start), _ms(end))
            cont = evaluate_continuation(imp, bars5, cfg)
            deriv = provider.derivatives_snapshot(imp.symbol)
            ready = combine_readiness(imp, cont, deriv, cfg)
            store.finalize(imp.symbol, imp.signal_time, cont, ready, deriv)
            processed += 1
            if ready.state in ("EARLY ENTRY", "PAPER-WATCH") or cont.confirmed:
                print(
                    "candidate",
                    json.dumps(
                        {
                            "symbol": imp.symbol,
                            "state": ready.state,
                            "tier": cont.tier,
                            "impulse_score": round(ready.impulse_score, 2),
                            "continuation_score": round(ready.continuation_score, 2),
                            "derivatives_score": None if ready.derivatives_score is None else round(ready.derivatives_score, 2),
                            "final_score": round(ready.final_score, 2),
                            "followthrough_30m_pct": round(cont.followthrough_return_pct, 3),
                            "mae_30m_pct": round(cont.mae_pct, 3),
                            "oi_change_1h_pct": deriv.oi_change_1h_pct,
                            "funding_rate": deriv.funding_rate,
                            "blockers": ready.blockers,
                        }
                    ),
                    flush=True,
                )
        except Exception as exc:
            continuation_errors += 1
            print("continuation_error", imp.symbol, repr(exc), flush=True)

    label_stats = process_labels(provider, store, cfg, now)
    try:
        promo_stats = process_promo_scan(store, now)
    except Exception as exc:
        promo_stats = {"performed": False, "error": repr(exc)}
        print("promo_scan_error", repr(exc), flush=True)
    summary = {
        "started_at": str(started),
        "finished_at": str(pd.Timestamp.now(tz="UTC")),
        "market_bucket": bucket,
        "scan_performed": scan_performed,
        "symbols_scanned": len(symbols),
        "last_market_scan_symbols": last_market_symbols,
        "new_impulses": new_count,
        "processed_continuations": processed,
        "scan_errors": scan_errors,
        "continuation_errors": continuation_errors,
        **label_stats,
        "promo_scan": promo_stats,
    }
    store.set_runtime("worker_heartbeat", summary)
    today = pd.Timestamp.now(tz="UTC").date().isoformat()
    last_snapshot = store.get_runtime("research_snapshot_date")
    if last_snapshot is None or last_snapshot.get("value") != today:
        snapshot = store.research_status()
        store.save_daily_snapshot(snapshot, today)
        store.set_runtime("research_snapshot_date", today)
    return summary


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--universe-limit", type=int, default=int(os.getenv("UNIVERSE_LIMIT", "150")))
    ap.add_argument("--sleep", type=int, default=int(os.getenv("WORKER_SLEEP_SECONDS", "60")))
    args = ap.parse_args()
    cfg = load_cfg()
    provider = BybitPublicProvider()
    store = SignalStore()
    historical_backfill_enabled = os.getenv("HISTORICAL_BACKFILL_ENABLED", "true").lower() in ("1", "true", "yes", "on")
    backfill = HistoricalBackfillRunner(provider, store, cfg) if historical_backfill_enabled else None
    backfill_batch_size = max(1, int(os.getenv("HISTORICAL_BACKFILL_SYMBOLS_PER_SCAN", "1")))
    while True:
        try:
            result = run_once(provider, store, cfg, args.universe_limit)
            if backfill is not None:
                try:
                    rebuild = _prepare_provenance_rebuild(backfill, store)
                    if rebuild is not None:
                        store.set_runtime("historical_backfill_rebuild", rebuild)
                        print("historical_backfill_rebuild", json.dumps(rebuild, default=str), flush=True)
                    backfill_result = backfill.run_batch(backfill_batch_size)
                    store.set_runtime("historical_backfill_last_batch", backfill_result)
                    store.set_runtime("historical_backfill_error", None)
                    print("historical_backfill", json.dumps(backfill_result, default=str), flush=True)
                except Exception as backfill_exc:
                    store.set_runtime(
                        "historical_backfill_error",
                        {"time": str(pd.Timestamp.now(tz="UTC")), "error": repr(backfill_exc)},
                    )
                    print("historical_backfill_error", repr(backfill_exc), flush=True)
            store.set_runtime("worker_error", None)
            print(result, flush=True)
        except Exception as exc:
            store.set_runtime("worker_error", {"time": str(pd.Timestamp.now(tz="UTC")), "error": repr(exc)})
            print("worker_error", repr(exc), flush=True)
        if args.once:
            break
        time.sleep(max(60, args.sleep))


if __name__ == "__main__":
    main()
