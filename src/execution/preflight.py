from __future__ import annotations

from dataclasses import dataclass, asdict
from math import floor


@dataclass
class ExecutionPlan:
    symbol: str
    equity_usdt: float
    entry_price: float
    hard_stop_pct: float
    stop_price: float
    target_risk_usdt: float
    risk_budget_usdt: float
    max_notional_usdt: float
    notional_usdt: float
    quantity: float
    estimated_loss_at_stop_usdt: float
    leverage: float
    allowed: bool
    blockers: list[str]

    def to_dict(self):
        return asdict(self)


def build_execution_plan(
    *,
    symbol: str,
    entry_price: float,
    equity_usdt: float,
    risk_limits: dict,
    min_notional_usdt: float | None = None,
    qty_step: float | None = None,
):
    blockers = []
    entry_price = float(entry_price)
    equity_usdt = float(equity_usdt)

    if entry_price <= 0:
        blockers.append("invalid_entry_price")
    if equity_usdt <= 0:
        blockers.append("invalid_equity")

    stop_pct = float(risk_limits.get("hard_stop_pct", 4.0))
    target_risk_fraction = float(risk_limits.get("target_risk_fraction_of_equity", 0.01))
    max_notional_fraction = float(risk_limits.get("max_notional_fraction_of_equity", 0.25))
    max_daily_loss_usdt = float(risk_limits.get("max_daily_loss_usdt", 0.25))
    max_leverage = float(risk_limits.get("max_leverage", 1.0))

    if stop_pct <= 0:
        blockers.append("invalid_stop_pct")

    target_risk_usdt = max(0.0, equity_usdt * target_risk_fraction)
    risk_budget_usdt = min(target_risk_usdt, max_daily_loss_usdt)
    max_notional_usdt = max(0.0, equity_usdt * max_notional_fraction * max_leverage)

    risk_limited_notional = 0.0
    if stop_pct > 0:
        risk_limited_notional = risk_budget_usdt / (stop_pct / 100.0)
    notional = min(max_notional_usdt, risk_limited_notional)

    quantity = 0.0 if entry_price <= 0 else notional / entry_price
    if qty_step is not None and qty_step > 0 and quantity > 0:
        quantity = floor(quantity / qty_step) * qty_step
        notional = quantity * entry_price

    estimated_loss = notional * (stop_pct / 100.0)
    stop_price = entry_price * (1.0 - stop_pct / 100.0) if entry_price > 0 else 0.0

    if min_notional_usdt is not None and notional + 1e-12 < float(min_notional_usdt):
        blockers.append("exchange_min_order_exceeds_risk_budget")

    if estimated_loss > risk_budget_usdt + 1e-9:
        blockers.append("risk_budget_exceeded")
    if notional > max_notional_usdt + 1e-9:
        blockers.append("max_notional_exceeded")
    if quantity <= 0:
        blockers.append("zero_quantity")

    return ExecutionPlan(
        symbol=symbol,
        equity_usdt=equity_usdt,
        entry_price=entry_price,
        hard_stop_pct=stop_pct,
        stop_price=stop_price,
        target_risk_usdt=target_risk_usdt,
        risk_budget_usdt=risk_budget_usdt,
        max_notional_usdt=max_notional_usdt,
        notional_usdt=notional,
        quantity=quantity,
        estimated_loss_at_stop_usdt=estimated_loss,
        leverage=max_leverage,
        allowed=len(blockers) == 0,
        blockers=blockers,
    )
