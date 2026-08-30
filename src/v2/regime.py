from __future__ import annotations

from .features import _atr_pct, _ret
from .models import MarketRegime


def detect_regime(btc15, eth15):
    btc = btc15.sort_index().tail(120)
    eth = eth15.sort_index().tail(120)

    b1, b4 = _ret(btc["close"], 4), _ret(btc["close"], 16)
    e1, e4 = _ret(eth["close"], 4), _ret(eth["close"], 16)
    ba, ea = _atr_pct(btc, 20), _atr_pct(eth, 20)

    b_ema = float(btc["close"].ewm(span=20, adjust=False).mean().iloc[-1])
    e_ema = float(eth["close"].ewm(span=20, adjust=False).mean().iloc[-1])
    b_px, e_px = float(btc["close"].iloc[-1]), float(eth["close"].iloc[-1])

    trend_score = 0.0
    trend_score += 0.25 if b_px > b_ema else -0.25
    trend_score += 0.25 if e_px > e_ema else -0.25
    trend_score += max(-0.25, min(0.25, b4 / max(ba * 4.0, 1e-6)))
    trend_score += max(-0.25, min(0.25, e4 / max(ea * 4.0, 1e-6)))

    high_vol = (ba + ea) / 2.0 >= 0.45
    if trend_score >= 0.35:
        name = "TREND_UP_HIGH_VOL" if high_vol else "TREND_UP"
    elif trend_score <= -0.35:
        name = "TREND_DOWN_HIGH_VOL" if high_vol else "TREND_DOWN"
    elif high_vol:
        name = "CHOP_HIGH_VOL"
    else:
        name = "CHOP"

    return MarketRegime(
        name=name,
        score=round(trend_score, 4),
        btc_ret_1h_pct=round(b1, 4),
        btc_ret_4h_pct=round(b4, 4),
        eth_ret_1h_pct=round(e1, 4),
        eth_ret_4h_pct=round(e4, 4),
        btc_atr_pct=round(ba, 4),
        eth_atr_pct=round(ea, 4),
        high_volatility=high_vol,
    )
