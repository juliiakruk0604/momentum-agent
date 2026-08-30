from __future__ import annotations

import os
import pandas as pd

from src.v2.provider import BybitV2Provider


HORIZONS = (5, 15, 30, 60)


def _utc(value):
    ts = pd.Timestamp(value)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _ms(ts):
    return int(_utc(ts).timestamp() * 1000)


def label_snapshot(provider, snapshot, horizon_minutes):
    snapshot_time = _utc(snapshot["snapshot_time"])
    entry_price = float(snapshot.get("price") or 0.0)
    if entry_price <= 0:
        raise RuntimeError("snapshot_entry_price_missing")

    # Never use the candle containing the decision because its OHLC contains
    # pre-decision information. Start from the next complete minute.
    path_start = snapshot_time.ceil("1min")
    path_end = path_start + pd.Timedelta(minutes=int(horizon_minutes))
    bars = provider.kline_range(
        snapshot["symbol"],
        "1",
        _ms(path_start),
        _ms(path_end + pd.Timedelta(minutes=1)),
        category="spot",
    )
    bars = bars[(bars.index >= path_start) & (bars.index < path_end)]
    if bars.empty:
        raise RuntimeError("future_1m_path_missing")

    highest = float(bars["high"].max())
    lowest = float(bars["low"].min())
    final_price = float(bars["close"].iloc[-1])

    mfe = (highest / entry_price - 1.0) * 100.0
    mae = (lowest / entry_price - 1.0) * 100.0
    final_return = (final_price / entry_price - 1.0) * 100.0

    return {
        "symbol": snapshot["symbol"],
        "snapshot_time": str(snapshot_time),
        "horizon_minutes": int(horizon_minutes),
        "entry_price": entry_price,
        "path_start": str(path_start),
        "path_end": str(path_end),
        "bars": int(len(bars)),
        "final_price": final_price,
        "final_return_pct": round(final_return, 6),
        "mfe_pct": round(mfe, 6),
        "mae_pct": round(mae, 6),
        "hit_0_5": mfe >= 0.5,
        "hit_1": mfe >= 1.0,
        "hit_2": mfe >= 2.0,
        "hit_5": mfe >= 5.0,
        "hit_10": mfe >= 10.0,
    }


class V22FlowLabeler:
    def __init__(self, store, provider=None):
        self.store = store
        self.provider = provider or BybitV2Provider()

    def run_batch(self, per_horizon=None):
        per_horizon = int(
            per_horizon
            or os.getenv("V22_LABELS_PER_HORIZON_PER_CYCLE", "8")
        )
        result = {
            "labeled": 0,
            "errors": [],
            "by_horizon": {},
        }

        for horizon in HORIZONS:
            candidates = self.store.v22_flow_label_candidates(
                horizon,
                limit=per_horizon,
            )
            labeled = 0
            errors = 0
            for snapshot in candidates:
                try:
                    label = label_snapshot(self.provider, snapshot, horizon)
                    self.store.upsert_v22_flow_label(label)
                    labeled += 1
                    result["labeled"] += 1
                except Exception as exc:
                    errors += 1
                    result["errors"].append({
                        "symbol": snapshot.get("symbol"),
                        "snapshot_time": str(snapshot.get("snapshot_time")),
                        "horizon": horizon,
                        "error": repr(exc)[:220],
                    })
            result["by_horizon"][str(horizon)] = {
                "eligible": len(candidates),
                "labeled": labeled,
                "errors": errors,
            }

        result["label_stats"] = self.store.v22_flow_label_stats()
        self.store.set_runtime("v22_flow_label_stats", result["label_stats"])
        return result
