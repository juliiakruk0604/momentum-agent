from __future__ import annotations

import math
import os
import time
import uuid
from dataclasses import dataclass, asdict
from decimal import Decimal, ROUND_DOWN, ROUND_UP

import requests

from src.bybit_account import _find_authenticated_base_url, _signed_get, _signed_post


@dataclass
class SpotExecutionPlan:
    symbol: str
    notional_usdt: float
    limit_price: float
    quantity: float
    stop_price: float
    take_profit_price: float
    spread_pct: float
    min_order_amt: float
    allowed: bool
    blockers: list[str]

    def to_dict(self):
        return asdict(self)


def _public_get(path: str, params: dict) -> dict:
    response = requests.get("https://api.bybit.com" + path, params=params, timeout=10)
    response.raise_for_status()
    data = response.json()
    if int(data.get("retCode", -1)) != 0:
        raise RuntimeError(f"Bybit public error {data.get('retCode')}: {data.get('retMsg')}")
    return data.get("result") or {}


def _floor_step(value: float, step: str) -> float:
    d = Decimal(str(value))
    s = Decimal(str(step))
    return float((d / s).to_integral_value(rounding=ROUND_DOWN) * s)


def _ceil_step(value: float, step: str) -> float:
    d = Decimal(str(value))
    s = Decimal(str(step))
    return float((d / s).to_integral_value(rounding=ROUND_UP) * s)


def spot_instrument(symbol: str) -> dict:
    result = _public_get("/v5/market/instruments-info", {"category": "spot", "symbol": symbol})
    rows = result.get("list") or []
    if not rows:
        raise RuntimeError("spot_instrument_not_found")
    return rows[0]


def spot_ticker(symbol: str) -> dict:
    result = _public_get("/v5/market/tickers", {"category": "spot", "symbol": symbol})
    rows = result.get("list") or []
    if not rows:
        raise RuntimeError("spot_ticker_not_found")
    return rows[0]


def unified_usdt_balance() -> float:
    base_url, _api_info, _attempts = _find_authenticated_base_url()
    result = _signed_get(
        base_url,
        "/v5/account/wallet-balance",
        {"accountType": "UNIFIED", "coin": "USDT"},
    ).get("result") or {}
    accounts = result.get("list") or []
    if not accounts:
        return 0.0
    coins = accounts[0].get("coin") or []
    for row in coins:
        if row.get("coin") == "USDT":
            return float(row.get("walletBalance") or 0.0)
    return 0.0



def funding_usdt_balance() -> float:
    base_url, _api_info, _attempts = _find_authenticated_base_url()
    result = _signed_get(
        base_url,
        "/v5/asset/transfer/query-account-coins-balance",
        {"accountType": "FUND", "coin": "USDT"},
    ).get("result") or {}
    for row in result.get("balance") or []:
        if row.get("coin") == "USDT":
            return float(row.get("transferBalance") or row.get("walletBalance") or 0.0)
    return 0.0


def ensure_unified_usdt(minimum: float) -> dict:
    current = unified_usdt_balance()
    if current >= minimum:
        return {"ok": True, "transferred": False, "unified_usdt": current}

    if os.getenv("LIVE_AUTO_TRANSFER", "false").lower() not in ("1", "true", "yes", "on"):
        return {"ok": False, "transferred": False, "unified_usdt": current, "reason": "auto_transfer_disabled"}

    transfer_amount = float(os.getenv("LIVE_TRANSFER_USDT", "5.5"))
    funding = funding_usdt_balance()
    if funding + 1e-9 < transfer_amount:
        return {
            "ok": False,
            "transferred": False,
            "unified_usdt": current,
            "funding_usdt": funding,
            "reason": "insufficient_funding_usdt",
        }

    base_url, _api_info, _attempts = _find_authenticated_base_url()
    transfer_id = str(uuid.uuid4())
    result = _signed_post(
        base_url,
        "/v5/asset/transfer/inter-transfer",
        {
            "transferId": transfer_id,
            "coin": "USDT",
            "amount": format(transfer_amount, ".16g"),
            "fromAccountType": "FUND",
            "toAccountType": "UNIFIED",
        },
    ).get("result") or {}
    time.sleep(1.0)
    updated = unified_usdt_balance()
    return {
        "ok": updated >= minimum,
        "transferred": True,
        "transfer_id": transfer_id,
        "transfer_status": result.get("status"),
        "amount_usdt": transfer_amount,
        "unified_usdt": updated,
        "funding_before_usdt": funding,
    }

def build_spot_plan(symbol: str, signal_price: float) -> SpotExecutionPlan:
    blockers: list[str] = []
    symbol = str(symbol).upper()
    if not symbol.endswith("USDT"):
        blockers.append("quote_not_usdt")

    inst = spot_instrument(symbol)
    if inst.get("status") != "Trading":
        blockers.append("spot_not_trading")
    if inst.get("quoteCoin") != "USDT":
        blockers.append("spot_quote_not_usdt")

    ticker = spot_ticker(symbol)
    ask = float(ticker.get("ask1Price") or ticker.get("lastPrice") or 0.0)
    bid = float(ticker.get("bid1Price") or ticker.get("lastPrice") or 0.0)
    if ask <= 0 or bid <= 0:
        blockers.append("invalid_orderbook")
        ask = max(ask, bid, float(signal_price or 0.0), 1e-12)

    spread_pct = max(0.0, (ask - bid) / ask * 100.0) if ask > 0 else 999.0
    max_spread = float(os.getenv("LIVE_MAX_SPREAD_PCT", "0.15"))
    if spread_pct > max_spread:
        blockers.append("spread_too_wide")

    extension_pct = 0.0
    if float(signal_price or 0.0) > 0:
        extension_pct = (ask / float(signal_price) - 1.0) * 100.0
        if extension_pct > float(os.getenv("LIVE_MAX_EXTENSION_PCT", "1.25")):
            blockers.append("entry_too_extended")

    lot = inst.get("lotSizeFilter") or {}
    price_filter = inst.get("priceFilter") or {}
    min_order_amt = float(lot.get("minOrderAmt") or 0.0)
    base_precision = str(lot.get("basePrecision") or "0.00000001")
    tick_size = str(price_filter.get("tickSize") or "0.00000001")

    notional_cap = float(os.getenv("LIVE_MAX_NOTIONAL_USDT", "5"))
    notional = max(min_order_amt, notional_cap)
    if min_order_amt > notional_cap + 1e-9:
        blockers.append("exchange_min_exceeds_cap")

    # Small aggressive IOC limit: bounded execution, never a market order.
    limit_price = _ceil_step(ask * 1.0005, tick_size)
    quantity = _floor_step(notional / limit_price, base_precision)
    actual_notional = quantity * limit_price
    if actual_notional + 1e-9 < min_order_amt:
        quantity = _ceil_step(min_order_amt / limit_price, base_precision)
        actual_notional = quantity * limit_price
    if actual_notional > notional_cap * 1.01:
        blockers.append("actual_notional_exceeds_cap")

    stop_pct = float(os.getenv("LIVE_HARD_STOP_PCT", "2.0"))
    tp_pct = float(os.getenv("LIVE_TAKE_PROFIT_PCT", "4.0"))
    stop_price = _floor_step(limit_price * (1.0 - stop_pct / 100.0), tick_size)
    take_profit_price = _ceil_step(limit_price * (1.0 + tp_pct / 100.0), tick_size)

    minimum_balance = float(os.getenv("LIVE_MIN_UNIFIED_USDT", "5.25"))
    transfer_state = ensure_unified_usdt(minimum_balance)
    balance = float(transfer_state.get("unified_usdt") or 0.0)
    if not transfer_state.get("ok"):
        blockers.append("insufficient_unified_usdt")
    if actual_notional > balance:
        blockers.append("notional_exceeds_balance")

    return SpotExecutionPlan(
        symbol=symbol,
        notional_usdt=round(actual_notional, 8),
        limit_price=limit_price,
        quantity=quantity,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        spread_pct=round(spread_pct, 6),
        min_order_amt=min_order_amt,
        allowed=len(blockers) == 0,
        blockers=blockers,
    )


def place_spot_plan(plan: SpotExecutionPlan) -> dict:
    if not plan.allowed:
        return {"submitted": False, "reason": "plan_blocked", "plan": plan.to_dict()}

    if os.getenv("LIVE_SPOT_EXECUTION", "false").lower() not in ("1", "true", "yes", "on"):
        return {"submitted": False, "reason": "execution_disabled", "plan": plan.to_dict()}

    # Hard safety invariants: category=spot and isLeverage=0 are not configurable.
    base_url, api_info, _attempts = _find_authenticated_base_url()
    permissions = api_info.get("permissions") or {}
    wallet_perms = set(permissions.get("Wallet") or [])
    spot_perms = set(permissions.get("Spot") or [])
    if "Withdraw" in wallet_perms:
        return {"submitted": False, "reason": "withdraw_permission_present", "plan": plan.to_dict()}
    if "SpotTrade" not in spot_perms:
        return {"submitted": False, "reason": "spot_trade_permission_missing", "plan": plan.to_dict()}

    link_id = f"ml-{int(time.time())}-{plan.symbol[:10]}"
    payload = {
        "category": "spot",
        "symbol": plan.symbol,
        "side": "Buy",
        "orderType": "Limit",
        "qty": format(plan.quantity, ".16g"),
        "price": format(plan.limit_price, ".16g"),
        "timeInForce": "IOC",
        "orderLinkId": link_id,
        "isLeverage": 0,
        "orderFilter": "Order",
        "takeProfit": format(plan.take_profit_price, ".16g"),
        "stopLoss": format(plan.stop_price, ".16g"),
        "tpOrderType": "Market",
        "slOrderType": "Market",
    }
    ack = _signed_post(base_url, "/v5/order/create", payload)
    result = ack.get("result") or {}
    order_id = result.get("orderId")
    time.sleep(1.0)

    status_data = _signed_get(
        base_url,
        "/v5/order/realtime",
        {
            "category": "spot",
            "symbol": plan.symbol,
            "orderId": order_id,
        },
    ).get("result") or {}
    rows = status_data.get("list") or []
    order = rows[0] if rows else {}

    execution_data = _signed_get(
        base_url,
        "/v5/execution/list",
        {
            "category": "spot",
            "symbol": plan.symbol,
            "orderId": order_id,
            "limit": 50,
        },
    ).get("result") or {}
    executions = execution_data.get("list") or []

    return {
        "submitted": True,
        "order_id": order_id,
        "order_link_id": link_id,
        "order_status": order.get("orderStatus"),
        "cum_exec_qty": order.get("cumExecQty"),
        "avg_price": order.get("avgPrice"),
        "executions": [
            {
                "exec_id": row.get("execId"),
                "exec_qty": row.get("execQty"),
                "exec_price": row.get("execPrice"),
                "exec_fee": row.get("execFee"),
            }
            for row in executions
        ],
        "plan": plan.to_dict(),
    }
