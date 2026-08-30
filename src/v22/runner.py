from __future__ import annotations

from dataclasses import dataclass, asdict


@dataclass
class RunnerState:
    status: str
    partial_hit: bool
    partial_exit_time: str | None
    partial_exit_price: float | None
    partial_fraction: float
    highest_price: float
    active_stop_price: float
    final_exit_time: str | None
    final_exit_price: float | None
    final_exit_reason: str | None
    total_pnl_usdt: float
    equity_value_usdt: float

    def to_dict(self):
        return asdict(self)


def evaluate_runner_path(
    bars1m,
    entry_time,
    entry_price,
    notional_usdt,
    initial_stop_pct,
    mark_price=None,
    fee_rate=0.001,
    entry_slippage_pct=0.05,
    exit_slippage_pct=0.05,
    partial_r=1.5,
    partial_fraction=0.5,
    trail_pct=1.2,
    breakeven_buffer_pct=0.30,
    max_hold_minutes=1440,
):
    if bars1m is None or bars1m.empty:
        raise RuntimeError("missing_1m_path")

    entry = float(entry_price) * (1.0 + float(entry_slippage_pct) / 100.0)
    notional = float(notional_usdt)
    qty = notional / entry
    entry_fee = notional * float(fee_rate)

    initial_stop = entry * (1.0 - float(initial_stop_pct) / 100.0)
    risk_distance = entry - initial_stop
    partial_level = entry + float(partial_r) * risk_distance
    remaining_qty = qty
    partial_qty = qty * float(partial_fraction)

    path = bars1m[bars1m.index >= entry_time].head(int(max_hold_minutes) + 1)
    if path.empty:
        raise RuntimeError("no_1m_bars_after_entry")

    partial_hit = False
    partial_exit_time = None
    partial_exit_price = None
    partial_proceeds = 0.0
    partial_fee = 0.0
    highest = entry
    active_stop = initial_stop
    final_time = None
    final_price = None
    final_reason = None

    for ts, row in path.iterrows():
        low = float(row["low"])
        high = float(row["high"])
        close = float(row["close"])

        # Conservative same-bar ordering: protective stop is evaluated before profit events.
        if low <= active_stop:
            final_time = ts
            final_price = active_stop * (1.0 - float(exit_slippage_pct) / 100.0)
            final_reason = "STOP" if not partial_hit else "RUNNER_TRAIL"
            break

        if not partial_hit and high >= partial_level:
            partial_hit = True
            partial_exit_time = ts
            partial_exit_price = partial_level * (1.0 - float(exit_slippage_pct) / 100.0)
            partial_proceeds = partial_qty * partial_exit_price
            partial_fee = partial_proceeds * float(fee_rate)
            remaining_qty -= partial_qty
            highest = max(highest, high)
            breakeven = entry * (1.0 + float(breakeven_buffer_pct) / 100.0)
            trailing = highest * (1.0 - float(trail_pct) / 100.0)
            active_stop = max(breakeven, trailing)
            # We cannot know intrabar ordering from OHLC. If the same candle also
            # traded through the newly active stop, assume the adverse valid path.
            if low <= active_stop:
                final_time = ts
                final_price = active_stop * (1.0 - float(exit_slippage_pct) / 100.0)
                final_reason = "PARTIAL_THEN_TRAIL_SAME_BAR"
                break
            continue

        if partial_hit:
            highest = max(highest, high)
            trailing = highest * (1.0 - float(trail_pct) / 100.0)
            breakeven = entry * (1.0 + float(breakeven_buffer_pct) / 100.0)
            new_stop = max(active_stop, breakeven, trailing)
            if low <= new_stop:
                active_stop = new_stop
                final_time = ts
                final_price = active_stop * (1.0 - float(exit_slippage_pct) / 100.0)
                final_reason = "RUNNER_TRAIL"
                break
            active_stop = new_stop

    if final_price is None and len(path) >= int(max_hold_minutes) + 1:
        final_time = path.index[-1]
        final_price = float(path.iloc[-1]["close"]) * (1.0 - float(exit_slippage_pct) / 100.0)
        final_reason = "TIME_EXIT"

    if final_price is not None:
        final_proceeds = remaining_qty * final_price
        final_fee = final_proceeds * float(fee_rate)
        total_proceeds = partial_proceeds - partial_fee + final_proceeds - final_fee
        pnl = total_proceeds - notional - entry_fee
        equity_value = total_proceeds
        status = "CLOSED"
    else:
        current = float(mark_price if mark_price is not None else path.iloc[-1]["close"])
        marked = current * (1.0 - float(exit_slippage_pct) / 100.0)
        remaining_value = remaining_qty * marked
        hypothetical_fee = remaining_value * float(fee_rate)
        total_value = partial_proceeds - partial_fee + remaining_value - hypothetical_fee
        pnl = total_value - notional - entry_fee
        equity_value = total_value
        status = "OPEN"

    return RunnerState(
        status=status,
        partial_hit=partial_hit,
        partial_exit_time=None if partial_exit_time is None else str(partial_exit_time),
        partial_exit_price=partial_exit_price,
        partial_fraction=float(partial_fraction),
        highest_price=highest,
        active_stop_price=active_stop,
        final_exit_time=None if final_time is None else str(final_time),
        final_exit_price=final_price,
        final_exit_reason=final_reason,
        total_pnl_usdt=round(pnl, 8),
        equity_value_usdt=round(equity_value, 8),
    )
