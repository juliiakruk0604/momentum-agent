from __future__ import annotations

import math
import pandas as pd

from .models import FeatureSnapshot


def _ret(series, bars):
    if len(series) <= bars or float(series.iloc[-bars-1]) <= 0:
        return 0.0
    return (float(series.iloc[-1]) / float(series.iloc[-bars-1]) - 1.0) * 100.0


def _atr_pct(x, window=20):
    if len(x) < window + 2:
        return 0.0
    prev_close = x["close"].shift(1)
    tr = pd.concat([
        x["high"] - x["low"],
        (x["high"] - prev_close).abs(),
        (x["low"] - prev_close).abs(),
    ], axis=1).max(axis=1)
    atr = float(tr.tail(window).mean())
    close = float(x["close"].iloc[-1])
    return 0.0 if close <= 0 else atr / close * 100.0


def _zscore(value, history):
    h = pd.Series(history).dropna()
    if len(h) < 5:
        return 0.0
    std = float(h.std(ddof=0))
    if std <= 1e-12:
        return 0.0
    return (float(value) - float(h.mean())) / std


def compute_features(symbol, bars15, btc15, eth15, turnover_24h=0.0):
    x = bars15.sort_index().tail(120)
    if len(x) < 60:
        raise RuntimeError("insufficient_15m_history")

    close = x["close"]
    price = float(close.iloc[-1])
    atr_pct = max(_atr_pct(x, 20), 1e-6)

    prior = x.iloc[-21:-1]
    prev_high = float(prior["high"].max())
    breakout_pct = (price / prev_high - 1.0) * 100.0
    breakout_atr = breakout_pct / atr_pct

    vols = x["volume"].iloc[-41:-1]
    vol_now = float(x["volume"].iloc[-1])
    volume_z = _zscore(vol_now, vols)
    vol_med = float(vols.tail(20).median())
    volume_ratio = 0.0 if vol_med <= 0 else vol_now / vol_med

    asset_ret_1h = _ret(close, 4)
    btc_ret_1h = _ret(btc15["close"].reindex(x.index, method="ffill"), 4)
    eth_ret_1h = _ret(eth15["close"].reindex(x.index, method="ffill"), 4)
    rs_pct = asset_ret_1h - (btc_ret_1h + eth_ret_1h) / 2.0
    rs_atr = rs_pct / atr_pct

    tp = (x["high"] + x["low"] + x["close"]) / 3.0
    v = x["volume"].tail(20)
    denom = float(v.sum())
    vwap = price if denom <= 0 else float((tp.tail(20) * v).sum() / denom)
    vwap_distance_atr = ((price / vwap - 1.0) * 100.0) / atr_pct if vwap > 0 else 0.0

    ma = close.rolling(20).mean()
    sd = close.rolling(20).std(ddof=0)
    width = ((ma + 2*sd) - (ma - 2*sd)) / ma.replace(0, pd.NA) * 100.0
    bb_width_pct = float(width.iloc[-1]) if pd.notna(width.iloc[-1]) else 0.0
    prior_width = width.iloc[-21:-1].dropna()
    prior_med = float(prior_width.median()) if len(prior_width) else bb_width_pct
    bb_width_expansion = 0.0 if prior_med <= 1e-12 else bb_width_pct / prior_med

    return FeatureSnapshot(
        symbol=symbol,
        price=price,
        atr_pct=atr_pct,
        ret_15m_pct=_ret(close, 1),
        ret_1h_pct=asset_ret_1h,
        ret_4h_pct=_ret(close, 16),
        breakout_atr=breakout_atr,
        volume_z=volume_z,
        volume_ratio=volume_ratio,
        rs_atr=rs_atr,
        vwap_distance_atr=vwap_distance_atr,
        bb_width_pct=bb_width_pct,
        bb_width_expansion=bb_width_expansion,
        turnover_24h=float(turnover_24h),
    )
