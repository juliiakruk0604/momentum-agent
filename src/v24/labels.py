from __future__ import annotations

import os


HORIZONS = (5, 15, 30, 60, 120, 300, 900, 1800)
LABEL_VERSION = "price_tick_v3_passage"


def _first_passage(rows, entry_ask, tp_pct, sl_pct):
    tp_level = entry_ask * (1.0 + float(tp_pct) / 100.0)
    sl_level = entry_ask * (1.0 - float(sl_pct) / 100.0)
    tp_ms = None
    sl_ms = None
    for row in rows:
        bid = float(row.get("best_bid") or 0.0)
        if bid <= 0:
            continue
        ts = int(row.get("snapshot_ms") or 0)
        if tp_ms is None and bid >= tp_level:
            tp_ms = ts
        if sl_ms is None and bid <= sl_level:
            sl_ms = ts
        if tp_ms is not None and sl_ms is not None:
            break
    return {
        "tp_pct": float(tp_pct),
        "sl_pct": float(sl_pct),
        "tp_ms": tp_ms,
        "sl_ms": sl_ms,
        "tp_before_sl": tp_ms is not None and (sl_ms is None or tp_ms < sl_ms),
        "sl_before_tp": sl_ms is not None and (tp_ms is None or sl_ms < tp_ms),
        "neither": tp_ms is None and sl_ms is None,
    }


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
        and int(r.get("snapshot_ms") or 0) <= end_ms + tolerance_ms
        and float(r.get("best_bid") or 0.0) > 0
    ]
    if not rows:
        raise RuntimeError("future_bid_path_missing")

    endpoint = min(
        rows,
        key=lambda r: abs(int(r.get("snapshot_ms") or 0) - end_ms),
    )
    endpoint_ms = int(endpoint["snapshot_ms"])
    if abs(endpoint_ms - end_ms) > tolerance_ms:
        raise RuntimeError("future_path_incomplete")

    rows = [r for r in rows if int(r["snapshot_ms"]) <= endpoint_ms]
    bids = [float(r["best_bid"]) for r in rows]
    final_bid = float(endpoint["best_bid"])
    last_ms = endpoint_ms
    high_bid = max(bids)
    low_bid = min(bids)

    final_return = (final_bid / entry_ask - 1.0) * 100.0
    mfe = (high_bid / entry_ask - 1.0) * 100.0
    mae = (low_bid / entry_ask - 1.0) * 100.0

    p035 = _first_passage(rows, entry_ask, 0.35, 0.15)
    p050 = _first_passage(rows, entry_ask, 0.50, 0.25)
    p100 = _first_passage(rows, entry_ask, 1.00, 0.50)
    p200 = _first_passage(rows, entry_ask, 2.00, 0.75)

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
        "label_version": LABEL_VERSION,
        "hit_0_1": mfe >= 0.10,
        "hit_0_25": mfe >= 0.25,
        "hit_0_5": mfe >= 0.50,
        "hit_1": mfe >= 1.00,
        "hit_2": mfe >= 2.00,
        "passage_035_015": p035,
        "passage_050_025": p050,
        "passage_100_050": p100,
        "passage_200_075": p200,
    }


class V24FeatureLabeler:
    def __init__(self, store):
        self.store = store

    def _ensure_version(self):
        tick_row = self.store.get_runtime("v24_price_tick_started")
        tick = None if tick_row is None else tick_row.get("value")
        if not isinstance(tick, dict) or not tick.get("started_ms"):
            return None

        version_row = self.store.get_runtime("v24_label_version")
        current = None if version_row is None else version_row.get("value")
        if current != LABEL_VERSION:
            self.store.clear_v24_feature_labels()
            self.store.set_runtime("v24_label_version", LABEL_VERSION)
            self.store.set_runtime("v24_label_rebuild", {
                "version": LABEL_VERSION,
                "started_ms": int(tick["started_ms"]),
                "reason": "switch_to_1s_price_tape",
            })
        return int(tick["started_ms"])

    def run_batch(self, per_horizon=None):
        per_horizon = int(
            per_horizon
            or os.getenv("V24_LABELS_PER_HORIZON_PER_CYCLE", "40")
        )
        min_snapshot_ms = self._ensure_version()
        if min_snapshot_ms is None:
            return {
                "status": "waiting_for_price_tape",
                "label_version": LABEL_VERSION,
                "labeled": 0,
                "errors": [],
                "by_horizon": {},
                "stats": self.store.v24_feature_label_stats(),
            }

        result = {
            "status": "running",
            "label_version": LABEL_VERSION,
            "min_snapshot_ms": min_snapshot_ms,
            "labeled": 0,
            "errors": [],
            "by_horizon": {},
        }

        for horizon in HORIZONS:
            candidates = self.store.v24_label_candidates(
                horizon,
                limit=per_horizon,
                min_snapshot_ms=min_snapshot_ms,
            )
            labeled = 0
            errors = 0
            for snapshot in candidates:
                try:
                    end_ms = int(snapshot["snapshot_ms"]) + int(horizon) * 1000
                    tolerance_ms = int(os.getenv("V24_LABEL_END_TOLERANCE_MS", "1500"))
                    path = self.store.v24_price_path(
                        str(snapshot["symbol"]),
                        int(snapshot["snapshot_ms"]),
                        end_ms + tolerance_ms,
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
