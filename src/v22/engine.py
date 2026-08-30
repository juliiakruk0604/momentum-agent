from __future__ import annotations

import os
from types import SimpleNamespace
import pandas as pd

from src.v2.provider import BybitV2Provider
from src.v2.regime import detect_regime
from src.v2.risk import risk_decision
from .fast_features import compute_fast_features
from .orderflow import flow_score


def _percentile_map(items, getter):
    pairs = []
    for item in items:
        try:
            pairs.append((item.symbol, float(getter(item))))
        except Exception:
            continue
    if not pairs:
        return {}
    ordered = sorted(pairs, key=lambda x: x[1])
    n = len(ordered)
    if n == 1:
        return {ordered[0][0]: 1.0}
    return {
        symbol: rank / float(n - 1)
        for rank, (symbol, _) in enumerate(ordered)
    }


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


def scan_fast_v22(provider=None, universe_limit=None, previous_scan=None):
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
    ticker_map = {
        t.get("symbol"): t
        for t in provider.tickers("spot")
        if t.get("symbol")
    }

    all_fast = []
    errors = []
    for symbol in symbols:
        try:
            bars = _closed_1m(provider.kline(symbol, "1", 91, category="spot"), now)
            f = compute_fast_features(symbol, bars, btc1, eth1)
            ticker = ticker_map.get(symbol) or {}
            live_price = float(ticker.get("lastPrice") or f.price)
            current_move = 0.0 if f.price <= 0 else (live_price / f.price - 1.0) * 100.0
            f.current_move_pct = round(current_move, 6)
            subminute_boost = 15.0 * min(
                max(current_move, 0.0) / max(float(f.realized_vol_20m_pct) * 2.0, 0.10),
                1.0,
            )
            f.coarse_score = round(min(100.0, float(f.coarse_score) + subminute_boost), 2)
            all_fast.append(f)
        except Exception as exc:
            errors.append({"symbol": symbol, "stage": "coarse", "error": repr(exc)[:160]})

    all_fast.sort(key=lambda x: x.coarse_score, reverse=True)
    coarse_count = sum(1 for x in all_fast if x.coarse_score >= coarse_gate)

    rs_pct = _percentile_map(all_fast, lambda x: x.rs_5m_pct)
    ret3_pct = _percentile_map(all_fast, lambda x: x.ret_3m_pct)
    volacc_pct = _percentile_map(all_fast, lambda x: x.volume_acceleration)
    score_pct = _percentile_map(all_fast, lambda x: x.coarse_score)

    fast_universe = []
    for f in all_fast:
        fast_universe.append({
            "symbol": f.symbol,
            "setup": "FAST_MOMENTUM_CONTEXT",
            "strategy_version": "2.5-base",
            "signal_time": f.signal_time,
            "signal_price": f.price,
            "score": round(float(f.coarse_score), 6),
            "score_kind": "coarse_fast_only",
            "regime": regime.name,
            "fast_features": f.to_dict(),
            "normalized_momentum": {
                "ret_3m_over_rv": round(
                    float(f.ret_3m_pct) / max(float(f.realized_vol_20m_pct) * (3.0 ** 0.5), 0.03),
                    6,
                ),
                "ret_5m_over_rv": round(
                    float(f.ret_5m_pct) / max(float(f.realized_vol_20m_pct) * (5.0 ** 0.5), 0.03),
                    6,
                ),
                "rs_5m_over_rv": round(
                    float(f.rs_5m_pct) / max(float(f.realized_vol_20m_pct) * (5.0 ** 0.5), 0.03),
                    6,
                ),
            },
            "cross_section": {
                "rs_5m_percentile": round(float(rs_pct.get(f.symbol, 0.0)), 6),
                "ret_3m_percentile": round(float(ret3_pct.get(f.symbol, 0.0)), 6),
                "volume_accel_percentile": round(float(volacc_pct.get(f.symbol, 0.0)), 6),
                "coarse_score_percentile": round(float(score_pct.get(f.symbol, 0.0)), 6),
                "composite_percentile": round(
                    0.40 * float(rs_pct.get(f.symbol, 0.0))
                    + 0.30 * float(ret3_pct.get(f.symbol, 0.0))
                    + 0.20 * float(volacc_pct.get(f.symbol, 0.0))
                    + 0.10 * float(score_pct.get(f.symbol, 0.0)),
                    6,
                ),
            },
            "action": "FAST_CONTEXT_ONLY",
            "live_execution": False,
        })

    enriched = []
    previous_by_symbol = {
        x.get("symbol"): x
        for x in ((previous_scan or {}).get("candidates") or [])
        if x.get("symbol")
    }

    for f in all_fast[: int(os.getenv("V22_MICRO_TOP_N", "8"))]:
        try:
            book = provider.orderbook(f.symbol, limit=25)
            trades = provider.recent_trades(f.symbol, limit=200, category="spot")
            flow, bookf, tradef = flow_score(book, trades)
        except Exception as exc:
            errors.append({"symbol": f.symbol, "stage": "flow", "error": repr(exc)[:160]})
            continue

        previous = previous_by_symbol.get(f.symbol) or {}
        prev_flow = float(previous.get("flow_score") or 0.0)
        prev_trade = previous.get("trade_flow") or {}
        prev_book = previous.get("book") or {}
        flow_score_delta = flow - prev_flow
        buy_ratio_delta = float(tradef.get("recent_buy_ratio", tradef.get("buy_ratio", 0.5))) - float(
            prev_trade.get("recent_buy_ratio", prev_trade.get("buy_ratio", 0.5))
        )
        book_imbalance_delta = float(bookf.get("book_imbalance") or 0.0) - float(
            prev_book.get("book_imbalance") or 0.0
        )

        combined = 0.60 * f.coarse_score + 0.40 * flow
        if f.volume_acceleration >= 1.8:
            combined += 4.0
        if f.price_acceleration > 0:
            combined += 3.0
        if f.current_move_pct > 0:
            combined += min(
                5.0,
                5.0 * f.current_move_pct / max(float(f.realized_vol_20m_pct) * 2.0, 0.10),
            )
        if f.rs_5m_pct > 0:
            combined += 3.0
        combined += min(max(flow_score_delta, 0.0) * 0.08, 4.0)
        combined += min(max(buy_ratio_delta, 0.0) * 20.0, 3.0)
        combined += min(max(book_imbalance_delta, 0.0) * 10.0, 2.0)
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
        if f.coarse_score < coarse_gate:
            blockers.append("coarse_score_low")
        if regime.name.startswith("TREND_DOWN"):
            blockers.append("market_trend_down")
        if f.ret_3m_pct <= 0:
            blockers.append("no_3m_momentum")
        if f.volume_acceleration < float(os.getenv("V22_MIN_VOLUME_ACCEL", "1.15")):
            blockers.append("volume_not_accelerating")
        if flow < flow_gate:
            blockers.append("orderflow_too_weak")
        recent_buy_ratio = float(tradef.get("recent_buy_ratio", tradef.get("buy_ratio", 0.5)))
        if recent_buy_ratio < float(os.getenv("V22_MIN_BUY_RATIO", "0.54")):
            blockers.append("taker_buy_ratio_low")
        if float(tradef.get("recent_notional") or 0.0) < float(os.getenv("V22_MIN_RECENT_NOTIONAL_USDT", "3000")):
            blockers.append("recent_trade_notional_low")
        if int(tradef.get("recent_trade_count") or 0) < int(os.getenv("V22_MIN_RECENT_TRADES", "15")):
            blockers.append("recent_trade_count_low")
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
            "normalized_momentum": {
                "ret_3m_over_rv": round(
                    float(f.ret_3m_pct) / max(float(f.realized_vol_20m_pct) * (3.0 ** 0.5), 0.03),
                    6,
                ),
                "ret_5m_over_rv": round(
                    float(f.ret_5m_pct) / max(float(f.realized_vol_20m_pct) * (5.0 ** 0.5), 0.03),
                    6,
                ),
                "rs_5m_over_rv": round(
                    float(f.rs_5m_pct) / max(float(f.realized_vol_20m_pct) * (5.0 ** 0.5), 0.03),
                    6,
                ),
            },
            "cross_section": {
                "rs_5m_percentile": round(float(rs_pct.get(f.symbol, 0.0)), 6),
                "ret_3m_percentile": round(float(ret3_pct.get(f.symbol, 0.0)), 6),
                "volume_accel_percentile": round(float(volacc_pct.get(f.symbol, 0.0)), 6),
                "coarse_score_percentile": round(float(score_pct.get(f.symbol, 0.0)), 6),
                "composite_percentile": round(
                    0.40 * float(rs_pct.get(f.symbol, 0.0))
                    + 0.30 * float(ret3_pct.get(f.symbol, 0.0))
                    + 0.20 * float(volacc_pct.get(f.symbol, 0.0))
                    + 0.10 * float(score_pct.get(f.symbol, 0.0)),
                    6,
                ),
            },
            "flow_acceleration": {
                "flow_score_delta": round(flow_score_delta, 6),
                "buy_ratio_delta": round(buy_ratio_delta, 6),
                "book_imbalance_delta": round(book_imbalance_delta, 6),
            },
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
        "coarse_candidates": coarse_count,
        "micro_sampled": min(len(all_fast), int(os.getenv("V22_MICRO_TOP_N", "8"))),
        "candidate_count": len(enriched),
        "fast_universe_count": len(fast_universe),
        "fast_universe": fast_universe,
        "candidates": enriched[:8],
        "errors": errors[:20],
        "live_execution": False,
    }
