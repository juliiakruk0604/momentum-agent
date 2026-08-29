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
from src.impulse import compute_impulse_candidates
from src.labeling import label_future_moves
from src.models import ImpulseSignal
from src.providers.bybit_public import BybitPublicProvider
from src.readiness import combine_readiness
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


def run_once(provider, store, cfg, universe_limit=100):
    started = pd.Timestamp.now(tz="UTC")
    btc = provider.kline("BTCUSDT", "15", 200)
    eth = provider.kline("ETHUSDT", "15", 200)
    symbols = provider.liquid_usdt_symbols(limit=universe_limit)
    new_count = 0
    scan_errors = 0

    for symbol in symbols:
        try:
            bars = provider.kline(symbol, "15", 200)
            impulses = compute_impulse_candidates(symbol, bars, btc, eth, cfg)
            if impulses:
                newest = impulses[-1]
                if pd.Timestamp.now(tz="UTC") - newest.available_time <= pd.Timedelta(minutes=45):
                    store.upsert_impulse(newest)
                    new_count += 1
        except Exception as exc:
            scan_errors += 1
            print("scan_error", symbol, repr(exc), flush=True)

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
            store.finalize(imp.symbol, imp.signal_time, cont, ready)
            processed += 1
            if ready.state in ("EARLY ENTRY", "PAPER-WATCH"):
                print("candidate", json.dumps({
                    "symbol": imp.symbol,
                    "state": ready.state,
                    "tier": cont.tier,
                    "followthrough_30m_pct": round(cont.followthrough_return_pct, 3),
                    "mae_30m_pct": round(cont.mae_pct, 3),
                    "oi_change_1h_pct": deriv.oi_change_1h_pct,
                    "funding_rate": deriv.funding_rate,
                    "blockers": ready.blockers,
                }), flush=True)
        except Exception as exc:
            continuation_errors += 1
            print("continuation_error", imp.symbol, repr(exc), flush=True)

    label_stats = process_labels(provider, store, cfg, now)
    summary = {
        "started_at": str(started),
        "finished_at": str(pd.Timestamp.now(tz="UTC")),
        "symbols": len(symbols),
        "new_impulses": new_count,
        "processed_continuations": processed,
        "scan_errors": scan_errors,
        "continuation_errors": continuation_errors,
        **label_stats,
    }
    store.set_runtime("worker_heartbeat", summary)
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
    while True:
        try:
            print(run_once(provider, store, cfg, args.universe_limit), flush=True)
        except Exception as exc:
            store.set_runtime("worker_error", {"time": str(pd.Timestamp.now(tz="UTC")), "error": repr(exc)})
            print("worker_error", repr(exc), flush=True)
        if args.once:
            break
        time.sleep(max(60, args.sleep))


if __name__ == "__main__":
    main()
