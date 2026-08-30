from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class SimulatedTrade:
    symbol: str
    entry_time: str
    entry_price: float
    exit_time: str | None
    exit_price: float | None
    exit_reason: str | None
    notional_usdt: float
    quantity: float
    entry_fee_usdt: float
    exit_fee_usdt: float
    pnl_usdt: float
    return_pct: float

    def to_dict(self):
        return asdict(self)


def find_barrier_exit(
    bars1m,
    entry_time,
    stop_price,
    target_price,
    exit_slippage_pct=0.05,
    max_hold_minutes=None,
):
    path = bars1m[bars1m.index >= entry_time]
    if max_hold_minutes is not None:
        path = path.head(int(max_hold_minutes) + 1)
    if path.empty:
        return None

    for ts, row in path.iterrows():
        low, high = float(row["low"]), float(row["high"])
        if low <= float(stop_price):
            return {
                "exit_time": ts,
                "exit_price": float(stop_price) * (1.0 - float(exit_slippage_pct) / 100.0),
                "exit_reason": "STOP",
            }
        if high >= float(target_price):
            return {
                "exit_time": ts,
                "exit_price": float(target_price) * (1.0 - float(exit_slippage_pct) / 100.0),
                "exit_reason": "TAKE_PROFIT",
            }

    if max_hold_minutes is not None and len(path) >= int(max_hold_minutes) + 1:
        return {
            "exit_time": path.index[-1],
            "exit_price": float(path.iloc[-1]["close"]) * (1.0 - float(exit_slippage_pct) / 100.0),
            "exit_reason": "TIME_EXIT",
        }
    return None


def simulate_long_path(
    symbol,
    bars1m,
    entry_time,
    entry_price,
    notional_usdt,
    stop_pct,
    target_pct,
    fee_rate=0.001,
    entry_slippage_pct=0.05,
    exit_slippage_pct=0.05,
    max_hold_minutes=720,
):
    if bars1m is None or bars1m.empty:
        raise RuntimeError("missing_1m_path")

    actual_entry = float(entry_price) * (1.0 + float(entry_slippage_pct) / 100.0)
    qty = float(notional_usdt) / actual_entry
    entry_fee = float(notional_usdt) * float(fee_rate)
    stop_price = actual_entry * (1.0 - float(stop_pct) / 100.0)
    target_price = actual_entry * (1.0 + float(target_pct) / 100.0)

    event = find_barrier_exit(
        bars1m,
        entry_time,
        stop_price,
        target_price,
        exit_slippage_pct=exit_slippage_pct,
        max_hold_minutes=max_hold_minutes,
    )
    if event is None:
        raise RuntimeError("no_1m_bars_after_entry")
    exit_price = float(event["exit_price"])
    exit_reason = str(event["exit_reason"])
    exit_time = event["exit_time"]

    gross_exit = qty * exit_price
    exit_fee = gross_exit * float(fee_rate)
    pnl = gross_exit - exit_fee - float(notional_usdt) - entry_fee
    denom = float(notional_usdt) + entry_fee
    ret = 0.0 if denom <= 0 else pnl / denom * 100.0

    return SimulatedTrade(
        symbol=symbol,
        entry_time=str(entry_time),
        entry_price=round(actual_entry, 10),
        exit_time=str(exit_time),
        exit_price=round(exit_price, 10),
        exit_reason=exit_reason,
        notional_usdt=round(float(notional_usdt), 8),
        quantity=round(qty, 12),
        entry_fee_usdt=round(entry_fee, 8),
        exit_fee_usdt=round(exit_fee, 8),
        pnl_usdt=round(pnl, 8),
        return_pct=round(ret, 6),
    )
