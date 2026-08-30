from __future__ import annotations

from dataclasses import dataclass, asdict
import pandas as pd


@dataclass
class FastFeatures:
    symbol: str
    signal_time: str
    price: float
    ret_1m_pct: float
    ret_3m_pct: float
    ret_5m_pct: float
    vol_z_1m: float
    volume_acceleration: float
    price_acceleration: float
    realized_vol_20m_pct: float
    compression_ratio: float
    rs_5m_pct: float
    coarse_score: float

    def to_dict(self):
        return asdict(self)


def _ret(series, bars):
    if len(series) <= bars:
        return 0.0
    a = float(series.iloc[-bars-1])
    b = float(series.iloc[-1])
    return 0.0 if a <= 0 else (b / a - 1.0) * 100.0


def _z(value, history):
    x = pd.Series(history).dropna()
    if len(x) < 10:
        return 0.0
    std = float(x.std(ddof=0))
    return 0.0 if std <= 1e-12 else (float(value) - float(x.mean())) / std


def compute_fast_features(symbol, bars1m, btc1m, eth1m):
    x = bars1m.sort_index().tail(90)
    if len(x) < 35:
        raise RuntimeError("insufficient_1m_history")

    close = x["close"]
    volume = x["volume"]
    price = float(close.iloc[-1])

    ret1 = _ret(close, 1)
    ret3 = _ret(close, 3)
    ret5 = _ret(close, 5)

    vol_now = float(volume.iloc[-1])
    vol_z = _z(vol_now, volume.iloc[-31:-1])
    recent3 = float(volume.tail(3).mean())
    prior10 = float(volume.iloc[-13:-3].mean())
    volume_accel = 0.0 if prior10 <= 1e-12 else recent3 / prior10

    r1 = close.pct_change() * 100.0
    recent_velocity = float(r1.tail(2).mean())
    prior_velocity = float(r1.iloc[-7:-2].mean())
    price_accel = recent_velocity - prior_velocity

    rv = float(r1.tail(20).std(ddof=0))
    recent_range = float((x["high"].tail(5).max() - x["low"].tail(5).min()) / price * 100.0)
    prior = x.iloc[-25:-5]
    prior_range = float((prior["high"].max() - prior["low"].min()) / max(float(prior["close"].iloc[-1]), 1e-12) * 100.0)
    compression_ratio = 0.0 if prior_range <= 1e-12 else recent_range / prior_range

    btc_aligned = btc1m["close"].reindex(x.index, method="ffill")
    eth_aligned = eth1m["close"].reindex(x.index, method="ffill")
    market5 = (_ret(btc_aligned, 5) + _ret(eth_aligned, 5)) / 2.0
    rs5 = ret5 - market5

    score = 0.0
    score += 20.0 * min(max(ret3, 0.0) / max(rv * 3.0, 0.15), 1.0)
    score += 20.0 * min(max(ret5, 0.0) / max(rv * 5.0, 0.25), 1.0)
    score += 22.0 * min(max(vol_z, 0.0) / 4.0, 1.0)
    score += 14.0 * min(max(volume_accel - 1.0, 0.0) / 2.0, 1.0)
    score += 12.0 * min(max(price_accel, 0.0) / max(rv, 0.05), 1.0)
    score += 12.0 * min(max(rs5, 0.0) / max(rv * 3.0, 0.15), 1.0)

    return FastFeatures(
        symbol=symbol,
        signal_time=str(x.index[-1]),
        price=price,
        ret_1m_pct=round(ret1, 6),
        ret_3m_pct=round(ret3, 6),
        ret_5m_pct=round(ret5, 6),
        vol_z_1m=round(vol_z, 6),
        volume_acceleration=round(volume_accel, 6),
        price_acceleration=round(price_accel, 6),
        realized_vol_20m_pct=round(rv, 6),
        compression_ratio=round(compression_ratio, 6),
        rs_5m_pct=round(rs5, 6),
        coarse_score=round(max(0.0, min(100.0, score)), 2),
    )
