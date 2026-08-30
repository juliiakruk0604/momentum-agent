from __future__ import annotations

import os
import pandas as pd

from .features import compute_features
from .provider import BybitV2Provider
from .regime import detect_regime
from .risk import microstructure, risk_decision
from .setups import evaluate_setups
from .diagnostics import diagnostic_strength, setup_blockers


def _closed_15m(frame, now=None):
    now = pd.Timestamp.now(tz="UTC") if now is None else pd.Timestamp(now)
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    x = frame.sort_index()
    if x.empty:
        return x
    # Bybit can include the currently-forming candle. Signals may only use data
    # that was fully available at decision time.
    last_open = pd.Timestamp(x.index[-1])
    if last_open + pd.Timedelta(minutes=15) > now:
        x = x.iloc[:-1]
    return x


def scan_v2(provider=None, universe_limit=None):
    provider = provider or BybitV2Provider()
    limit = int(universe_limit or os.getenv("V2_UNIVERSE_LIMIT", "40"))
    min_turnover = float(os.getenv("V2_MIN_TURNOVER_USDT", "5000000"))
    min_score = float(os.getenv("V2_MIN_SCORE", "65"))

    now = pd.Timestamp.now(tz="UTC")
    btc = _closed_15m(provider.kline("BTCUSDT", "15", 121, category="spot"), now)
    eth = _closed_15m(provider.kline("ETHUSDT", "15", 121, category="spot"), now)
    regime = detect_regime(btc, eth)

    ticker_map = {
        t.get("symbol"): t
        for t in provider.tickers("spot")
        if t.get("symbol")
    }
    symbols = provider.liquid_spot_usdt_symbols(limit=limit, min_turnover=min_turnover)

    raw_candidates = []
    near_misses = []
    errors = []
    scanned = 0

    for symbol in symbols:
        try:
            bars = _closed_15m(provider.kline(symbol, "15", 121, category="spot"), now)
            ticker = ticker_map.get(symbol) or {}
            turnover = float(ticker.get("turnover24h") or 0.0)
            f = compute_features(symbol, bars, btc, eth, turnover)
            setups = evaluate_setups(f, regime)
            for candidate in setups:
                if candidate.score >= min_score:
                    raw_candidates.append(candidate)
            if not setups:
                blockers = setup_blockers(f, regime)
                near_misses.append({
                    "symbol": symbol,
                    "diagnostic_score": diagnostic_strength(f, regime),
                    "regime": regime.name,
                    "blockers": blockers,
                    "features": f.to_dict(),
                    "never_authorizes_trade": True,
                })
            scanned += 1
        except Exception as exc:
            errors.append({"symbol": symbol, "error": repr(exc)[:180]})

    raw_candidates.sort(key=lambda c: c.score, reverse=True)

    # Only expensive microstructure + derivative enrichment for top candidates.
    enriched = []
    for candidate in raw_candidates[:12]:
        try:
            micro = microstructure(provider, candidate.symbol)
        except Exception as exc:
            micro = {"ok": False, "spread_pct": None, "depth_usdt": 0.0, "reason": repr(exc)[:120]}

        oi_change = provider.linear_oi_change_1h_pct(candidate.symbol)
        funding = provider.linear_funding_rate(candidate.symbol)
        adjusted = float(candidate.score)
        derivative_reasons = []
        if oi_change is not None:
            if oi_change > 0:
                adjusted += min(6.0, oi_change * 1.5)
                derivative_reasons.append("perp_oi_support")
            elif oi_change < -2:
                adjusted -= min(6.0, abs(oi_change))
                derivative_reasons.append("perp_oi_divergence")
        if funding is not None and abs(funding) > 0.0015:
            adjusted -= 8.0
            derivative_reasons.append("crowded_funding")

        candidate.score = round(max(0.0, min(100.0, adjusted)), 2)
        candidate.reasons.extend(derivative_reasons)
        decision = risk_decision(candidate, micro, equity_usdt=15.0)

        enriched.append({
            **candidate.to_dict(),
            "microstructure": micro,
            "perp_features": {
                "oi_change_1h_pct": oi_change,
                "funding_rate": funding,
                "auxiliary_only": True,
            },
            "risk": decision.to_dict(),
            "action": "SHADOW_READY" if decision.allowed and candidate.score >= min_score else "WATCH",
        })

    enriched.sort(key=lambda x: x["score"], reverse=True)
    return {
        "engine": "MomentumAgentV2",
        "mode": "SHADOW_ONLY",
        "strategy_version": os.getenv("V2_STRATEGY_VERSION", "2.1"),
        "generated_at": str(pd.Timestamp.now(tz="UTC")),
        "spot_is_execution_truth": True,
        "perp_features_auxiliary_only": True,
        "regime": regime.to_dict(),
        "universe_size": len(symbols),
        "symbols_scanned": scanned,
        "errors": errors[:20],
        "candidate_count": len(enriched),
        "candidates": enriched[:8],
        "near_misses": sorted(
            near_misses,
            key=lambda x: x.get("diagnostic_score", 0.0),
            reverse=True,
        )[:8],
    }
