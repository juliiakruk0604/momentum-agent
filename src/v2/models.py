from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class MarketRegime:
    name: str
    score: float
    btc_ret_1h_pct: float
    btc_ret_4h_pct: float
    eth_ret_1h_pct: float
    eth_ret_4h_pct: float
    btc_atr_pct: float
    eth_atr_pct: float
    high_volatility: bool

    def to_dict(self):
        return asdict(self)


@dataclass
class FeatureSnapshot:
    symbol: str
    price: float
    atr_pct: float
    ret_15m_pct: float
    ret_1h_pct: float
    ret_4h_pct: float
    breakout_atr: float
    volume_z: float
    volume_ratio: float
    rs_atr: float
    vwap_distance_atr: float
    bb_width_pct: float
    bb_width_expansion: float
    turnover_24h: float

    def to_dict(self):
        return asdict(self)


@dataclass
class SetupCandidate:
    symbol: str
    setup: str
    score: float
    signal_price: float
    stop_pct: float
    target_pct: float
    regime: str
    reasons: list[str]
    features: dict

    def to_dict(self):
        return asdict(self)


@dataclass
class RiskDecision:
    allowed: bool
    notional_usdt: float
    risk_usdt: float
    stop_pct: float
    target_pct: float
    blockers: list[str]
    execution_cost_pct: float = 0.0
    net_risk_pct: float = 0.0
    net_reward_pct: float = 0.0
    net_rr: float = 0.0

    def to_dict(self):
        return asdict(self)
