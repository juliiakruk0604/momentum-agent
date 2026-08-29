from __future__ import annotations
from dataclasses import dataclass
from typing import Optional
import pandas as pd

@dataclass
class ImpulseSignal:
    symbol: str
    signal_time: pd.Timestamp
    available_time: pd.Timestamp
    signal_price: float
    base_price: float
    move_from_base_pct: float
    breakout_pct: float
    volume_ratio: float
    relative_strength_pct: float
    impulse_score: float

@dataclass
class ContinuationResult:
    symbol: str
    signal_time: pd.Timestamp
    confirmation_time: pd.Timestamp
    followthrough_return_pct: float
    close_above_signal_ratio: float
    mae_pct: float
    progress_efficiency: float
    continuation_score: float
    confirmed: bool
    reason: str
    tier: str = "REJECTED"

@dataclass
class DerivativesSnapshot:
    oi_change_1h_pct: Optional[float] = None
    funding_rate: Optional[float] = None
    taker_buy_sell_ratio: Optional[float] = None
    short_liquidation_usd_1h: Optional[float] = None
    long_liquidation_usd_1h: Optional[float] = None
    source: str = "NONE"

@dataclass
class TradeReadiness:
    symbol: str
    impulse_score: float
    continuation_score: float
    derivatives_score: Optional[float]
    final_score: float
    state: str
    blockers: list[str]

@dataclass
class FutureMoveLabel:
    symbol: str
    signal_time: pd.Timestamp
    horizon_minutes: int
    mfe_pct: float
    mae_pct: float
    close_return_pct: float
    hit_5_before_invalidation: bool
    hit_10_before_invalidation: bool
    hit_20_before_invalidation: bool
    hit_30_before_invalidation: bool
    time_to_mfe_minutes: int
    invalidated: bool
