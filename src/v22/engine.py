from __future__ import annotations

import os
from types import SimpleNamespace
import pandas as pd

from src.v2.provider import BybitV2Provider
from src.v2.regime import detect_regime
from src.v2.risk import risk_decision
from .fast_features import compute_fast_features
from .orderflow import flow_score


def _closed_1m(frame, now=None):
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    x = frame.sort_index()
    if x.empty:
        return x
    last_open = pd.Timestamp(x.index[-1])
    if last_open + pd.Timedelta(minutes=1) > now:
        x = x.iloc[:-1]
    return x


def scan_fast_v22(provider=None, universe_limit=None):
    provider = provider or BybitV2Provider()
    now = pd.Timestamp.now(tz="UTC")
    universe_limit = int(universe_limit or os.getenv("V22_UNIVERSE_LIMIT", "25"))
    min_turnover = float(os.getenv("V22_MIN_TURNOVER_USDT", "5000000"))
    coarse_gate = float(os.getenv("V22_COARSE_SCORE", "55"))
    final_gate = float(os.getenv("V22_FINAL_SCORE", "76"))
    flow_gate = float(os.getenv("V22_FLOW_SCORE", "48"))

    btc1 = _closed_1m(provider.kline("BTCUSDT", "1", 91, category="spot"), now)
    eth1 = _closed_1m(provider.kline("ETHUSDT", "1", 91, category="spot"), now)
    btc15 = provider.kline("BTCUSDT", "15", 121, category="spot")
    eth15 = provider.kline("ETHUSDT", "15", 121, category="spot")
    # Regime code itself uses only trailing closed context in normal live cadence.
    regime = detect_regime(btc15.iloc[:-1] if len(btc15) else btc15, eth15.iloc[:-1] if len(eth15) else eth15)

    symbols = provider.liquid_spot_usdt_symbols(
        limit=universe_limit,
        min_turnover=min_turnover,
    )

    coarse = []
    errors = []
    for symbol in symbols:
        try:
            bars = _closed_1m(provider.kline(symbol, "1", 91, category="spot"), now)
            f = compute_fast_features(symbol, bars, btc1, eth1)
            if f.coarse_score >= coarse_gate:
                coarse.append(f)
        except Exception as exc:
            errors.append({"symbol": symbol, "stage": "coarse", "error": repr(exc)[:160]})

    coarse.sort(key=lambda x: x.coarse_score, reverse=True)
    enriched = []

    for f in coarse[: int(os.getenv("V22_MICRO_TOP_N", "8"))]:
        try:
            book = provider.orderbook(f.symbol, limit=25)
            trades = provider.recent_trades(f.symbol, limit=200, category="spot")
            flow, bookf, tradef = flow_score(book, trades)
        except Exception as exc:
            errors.append({"symbol": f.symbol, "stage": "flow", "error": repr(exc)[:160]})
            continue

        combined = 0.60 * f.coarse_score + 0.40 * flow
        if f.volume_acceleration >= 1.8:
            combined += 4.0
        if f.price_acceleration > 0:
            combined += 3.0
        if f.rs_5m_pct > 0:
            combined += 3.0
        combined = round(min(100.0, combined), 2)

        rv = max(float(f.realized_vol_20m_pct), 0.03)
        stop_pct = max(0.65, min(1.50, rv * 3.5))
        provisional_target = max(2.0, stop_pct * 2.5)

        candidate_obj = SimpleNamespace(
            stop_pct=stop_pct,
            target_pct=provisional_target,
        )
        micro = {
            "ok": bool(bookf.get("ok")),
            "best_bid": bookf.get("best_bid"),
            "best_ask": bookf.get("best_ask"),
            "spread_pct": bookf.get("spread_pct"),
            "depth_usdt": bookf.get("depth_usdt"),
        }
        decision = risk_decision(candidate_obj, micro, equity_usdt=15.0)

        blockers = []
        if regime.name.startswith("TREND_DOWN"):
            blockers.append("market_trend_down")
        if f.ret_3m_pct <= 0:
            blockers.append("no_3m_momentum")
        if f.volume_acceleration < float(os.getenv("V22_MIN_VOLUME_ACCEL", "1.15")):
            blockers.append("volume_not_accelerating")
        if flow < flow_gate:
            blockers.append("orderflow_too_weak")
        if tradef.get("buy_ratio", 0.5) < float(os.getenv("V22_MIN_BUY_RATIO", "0.54")):
            blockers.append("taker_buy_ratio_low")
        if combined < final_gate:
            blockers.append("score_below_gate")
        blockers.extend(decision.blockers)

        enriched.append({
            "symbol": f.symbol,
            "setup": "SENSITIVE_ACCELERATION",
            "strategy_version": "2.2",
            "signal_time": f.signal_time,
            "signal_price": f.price,
            "score": combined,
            "regime": regime.name,
            "fast_features": f.to_dict(),
            "flow_score": flow,
            "book": bookf,
            "trade_flow": tradef,
            "risk": decision.to_dict(),
            "initial_stop_pct": stop_pct,
            "runner": {
                "partial_r": float(os.getenv("V22_PARTIAL_R", "1.5")),
                "partial_fraction": float(os.getenv("V22_PARTIAL_FRACTION", "0.5")),
                "trail_pct": float(os.getenv("V22_TRAIL_PCT", "1.2")),
                "max_hold_minutes": int(os.getenv("V22_MAX_HOLD_MINUTES", "1440")),
            },
            "blockers": blockers,
            "action": "SHADOW_READY" if not blockers else "WATCH",
        })

    enriched.sort(key=lambda x: x["score"], reverse=True)
    return {
        "engine": "MomentumAgentV2.2",
        "mode": "FAST_SHADOW_CHALLENGER",
        "generated_at": str(now),
        "regime": regime.to_dict(),
        "universe_size": len(symbols),
        "coarse_candidates": len(coarse),
        "candidate_count": len(enriched),
        "candidates": enriched[:8],
        "errors": errors[:20],
        "live_execution": False,
    }
