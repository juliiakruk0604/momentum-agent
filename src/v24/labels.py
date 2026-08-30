from __future__ import annotations

import os


HORIZONS = (5, 15, 30, 60)


def label_from_path(snapshot: dict, path: list[dict], horizon_seconds: int):
    entry_ask = float(snapshot.get("best_ask") or 0.0)
    if entry_ask <= 0:
        raise RuntimeError("entry_ask_missing")
    if not path:
        raise RuntimeError("future_path_missing")

    start_ms = int(snapshot.get("snapshot_ms") or 0)
    end_ms = start_ms + int(horizon_seconds) * 1000
    tolerance_ms = int(os.getenv("V24_LABEL_END_TOLERANCE_MS", "7000"))

    rows = [
        r for r in path
        if int(r.get("snapshot_ms") or 0) > start_ms
        and int(r.get("snapshot_ms") or 0) <= end_ms
        and float(r.get("best_bid") or 0.0) > 0
    ]
    if not rows:
        raise RuntimeError("future_bid_path_missing")

    last_ms = int(rows[-1]["snapshot_ms"])
    if last_ms < end_ms - tolerance_ms:
        raise RuntimeError("future_path_incomplete")

    bids = [float(r["best_bid"]) for r in rows]
    final_bid = bids[-1]
    high_bid = max(bids)
    low_bid = min(bids)

    final_return = (final_bid / entry_ask - 1.0) * 100.0
    mfe = (high_bid / entry_ask - 1.0) * 100.0
    mae = (low_bid / entry_ask - 1.0) * 100.0

    return {
        "symbol": str(snapshot["symbol"]),
        "snapshot_ms": start_ms,
        "horizon_seconds": int(horizon_seconds),
        "entry_ask": entry_ask,
        "final_bid": final_bid,
        "final_bid_return_pct": round(final_return, 8),
        "mfe_bid_pct": round(mfe, 8),
        "mae_bid_pct": round(mae, 8),
        "path_points": len(rows),
        "last_path_ms": last_ms,
        "hit_0_1": mfe >= 0.10,
        "hit_0_25": mfe >= 0.25,
        "hit_0_5": mfe >= 0.50,
        "hit_1": mfe >= 1.00,
        "hit_2": mfe >= 2.00,
    }


class V24FeatureLabeler:
    def __init__(self, store):
        self.store = store

    def run_batch(self, per_horizon=None):
        per_horizon = int(
            per_horizon
            or os.getenv("V24_LABELS_PER_HORIZON_PER_CYCLE", "40")
        )
        result = {"labeled": 0, "errors": [], "by_horizon": {}}

        for horizon in HORIZONS:
            candidates = self.store.v24_label_candidates(
                horizon,
                limit=per_horizon,
            )
            labeled = 0
            errors = 0
            for snapshot in candidates:
                try:
                    end_ms = int(snapshot["snapshot_ms"]) + int(horizon) * 1000
                    path = self.store.v24_feature_path(
                        str(snapshot["symbol"]),
                        int(snapshot["snapshot_ms"]),
                        end_ms,
                    )
                    label = label_from_path(snapshot, path, horizon)
                    self.store.upsert_v24_feature_label(label)
                    labeled += 1
                    result["labeled"] += 1
                except Exception as exc:
                    errors += 1
                    result["errors"].append({
                        "symbol": snapshot.get("symbol"),
                        "snapshot_ms": snapshot.get("snapshot_ms"),
                        "horizon_seconds": horizon,
                        "error": repr(exc)[:220],
                    })
            result["by_horizon"][str(horizon)] = {
                "eligible": len(candidates),
                "labeled": labeled,
                "errors": errors,
            }

        result["stats"] = self.store.v24_feature_label_stats()
        self.store.set_runtime("v24_feature_label_stats", result["stats"])
        return result
