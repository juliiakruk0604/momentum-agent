from __future__ import annotations

import json
import os
import time

import pandas as pd

from src.store import SignalStore
from src.v2.engine import scan_v2
from src.v2.shadow import process_v2_shadow
from src.v22.engine import scan_fast_v22
from src.v22.shadow import process_v22_shadow


def _fast_bucket(now=None):
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    return now.floor("1min").isoformat()


def _bucket(now=None):
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    return now.floor("15min").isoformat()


def main():
    store = SignalStore()
    sleep_seconds = max(10, int(os.getenv("V2_MARKET_LOOP_SECONDS", "20")))
    universe = int(os.getenv("V2_UNIVERSE_LIMIT", "40"))
    force_boot = os.getenv("V2_FORCE_BOOT_SCAN", "true").lower() in ("1","true","yes","on")
    first = True
    last_action_fingerprint = None
    last_v22_fingerprint = None

    print("V2_MARKET_SERVICE_START", json.dumps({
        "loop_seconds": sleep_seconds,
        "universe": universe,
        "live_execution": False,
    }), flush=True)

    while True:
        started = pd.Timestamp.now(tz="UTC")
        bucket = _bucket(started)
        heartbeat = {
            "started_at": str(started),
            "bucket": bucket,
            "scan": None,
            "shadow": None,
            "fast_scan": None,
            "v22_shadow": None,
            "error": None,
            "live_execution": False,
        }

        try:
            last = store.get_runtime("v2_scan_bucket")
            should_scan = first and force_boot
            should_scan = should_scan or last is None or last.get("value") != bucket

            if should_scan:
                result = scan_v2(universe_limit=universe)
                store.set_runtime("v2_scan", result)
                store.set_runtime("v2_scan_bucket", bucket)
                heartbeat["scan"] = {
                    "performed": True,
                    "candidate_count": result.get("candidate_count"),
                    "regime": (result.get("regime") or {}).get("name"),
                    "symbols_scanned": result.get("symbols_scanned"),
                    "near_misses": (result.get("near_misses") or [])[:3],
                }
                print("V2_MARKET_SCAN", json.dumps({
                    "bucket": bucket,
                    "regime": result.get("regime"),
                    "candidate_count": result.get("candidate_count"),
                    "top": (result.get("candidates") or [])[:3],
                    "near_misses": (result.get("near_misses") or [])[:3],
                }, default=str), flush=True)
            else:
                heartbeat["scan"] = {"performed": False}

            fast_bucket = _fast_bucket(started)
            fast_last = store.get_runtime("v22_fast_scan_bucket")
            should_fast_scan = first or fast_last is None or fast_last.get("value") != fast_bucket

            if should_fast_scan:
                fast_result = scan_fast_v22(
                    universe_limit=int(os.getenv("V22_UNIVERSE_LIMIT", "25"))
                )
                store.set_runtime("v22_fast_scan", fast_result)
                store.set_runtime("v22_fast_scan_bucket", fast_bucket)
                for candidate in fast_result.get("candidates") or []:
                    snapshot = {
                        **candidate,
                        "snapshot_time": fast_result.get("generated_at"),
                        "generated_at": fast_result.get("generated_at"),
                    }
                    store.upsert_v22_flow_snapshot(snapshot)
                store.set_runtime("v22_flow_snapshot_stats", store.v22_flow_snapshot_stats())
                heartbeat["fast_scan"] = {
                    "performed": True,
                    "candidate_count": fast_result.get("candidate_count"),
                    "coarse_candidates": fast_result.get("coarse_candidates"),
                    "regime": (fast_result.get("regime") or {}).get("name"),
                    "top": (fast_result.get("candidates") or [])[:3],
                }
                print("V22_FAST_SCAN", json.dumps(heartbeat["fast_scan"], default=str), flush=True)
            else:
                heartbeat["fast_scan"] = {"performed": False}

            v22_shadow = process_v22_shadow(store)
            heartbeat["v22_shadow"] = {
                "equity": v22_shadow.get("current_equity_usdt"),
                "net_pnl": v22_shadow.get("net_pnl_usdt"),
                "open_position": v22_shadow.get("open_position"),
                "closed_trades": v22_shadow.get("closed_trades"),
                "last_action": v22_shadow.get("last_action"),
                "last_rejection": v22_shadow.get("last_rejection"),
            }

            v22_fingerprint = json.dumps({
                "action": v22_shadow.get("last_action"),
                "rejection": v22_shadow.get("last_rejection"),
                "equity": v22_shadow.get("current_equity_usdt"),
            }, sort_keys=True, default=str)
            if v22_fingerprint != last_v22_fingerprint:
                print("V22_SHADOW_STATE", json.dumps(heartbeat["v22_shadow"], default=str), flush=True)
                last_v22_fingerprint = v22_fingerprint

            shadow = process_v2_shadow(store)
            heartbeat["shadow"] = {
                "equity": shadow.get("current_equity_usdt"),
                "net_pnl": shadow.get("net_pnl_usdt"),
                "open_position": shadow.get("open_position"),
                "closed_trades": shadow.get("closed_trades"),
                "last_action": shadow.get("last_action"),
                "last_rejection": shadow.get("last_rejection"),
            }

            fingerprint = json.dumps({
                "action": shadow.get("last_action"),
                "rejection": shadow.get("last_rejection"),
                "equity": shadow.get("current_equity_usdt"),
            }, sort_keys=True, default=str)
            if fingerprint != last_action_fingerprint:
                print("V2_SHADOW_STATE", json.dumps(heartbeat["shadow"], default=str), flush=True)
                last_action_fingerprint = fingerprint

            store.set_runtime("v2_market_error", None)
        except Exception as exc:
            heartbeat["error"] = repr(exc)
            store.set_runtime("v2_market_error", {
                "time": str(pd.Timestamp.now(tz="UTC")),
                "error": repr(exc),
            })
            print("V2_MARKET_ERROR", repr(exc), flush=True)

        heartbeat["finished_at"] = str(pd.Timestamp.now(tz="UTC"))
        store.set_runtime("v2_market_heartbeat", heartbeat)
        first = False
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
