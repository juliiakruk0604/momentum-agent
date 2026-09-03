from __future__ import annotations

import json
import math
import os
import time
import uuid
from decimal import Decimal, ROUND_DOWN, ROUND_UP

import requests

from src.bybit_account import (
    _find_authenticated_base_url,
    _safe_error,
    _signed_get,
    _signed_post,
    account_diagnostic,
)


MAX_NOTIONAL_USDT = min(2.0, float(os.getenv("MANUAL_MAX_NOTIONAL_USDT", "2")))
ORDER_LINK_ID = os.getenv("MANUAL_ORDER_ID", "cg-spot-20260903-001")[:36]
EXCLUDED_BASES = {
    "USDC", "USDE", "DAI", "FDUSD", "TUSD", "USDD", "USD1", "PYUSD",
    "EUR", "EURT", "EURI", "USDT",
}


def emit(event: str, **payload) -> None:
    print(json.dumps({"event": event, **payload}, ensure_ascii=False, default=str), flush=True)


def public_get(base_url: str, path: str, params: dict) -> dict:
    response = requests.get(base_url + path, params=params, timeout=12)
    response.raise_for_status()
    data = response.json()
    if int(data.get("retCode", -1)) != 0:
        raise RuntimeError(f"Bybit public error {data.get('retCode')}: {data.get('retMsg')}")
    return data.get("result") or {}


def signed_get(base_url: str, path: str, params: dict) -> dict:
    # The helper signs a sorted query string. Preserve the same ordering in the
    # actual request so Bybit verifies the identical payload.
    return _signed_get(base_url, path, dict(sorted(params.items())))


def floor_step(value: float, step: str) -> float:
    d = Decimal(str(value))
    s = Decimal(str(step))
    return float((d / s).to_integral_value(rounding=ROUND_DOWN) * s)


def ceil_step(value: float, step: str) -> float:
    d = Decimal(str(value))
    s = Decimal(str(step))
    return float((d / s).to_integral_value(rounding=ROUND_UP) * s)


def get_instruments(base_url: str) -> dict[str, dict]:
    result = public_get(base_url, "/v5/market/instruments-info", {"category": "spot"})
    rows = result.get("list") or []
    return {
        str(row.get("symbol")): row
        for row in rows
        if row.get("status") == "Trading" and row.get("quoteCoin") == "USDT"
    }


def kline_metrics(base_url: str, symbol: str, last: float) -> dict | None:
    result = public_get(
        base_url,
        "/v5/market/kline",
        {"category": "spot", "symbol": symbol, "interval": "15", "limit": 40},
    )
    rows = result.get("list") or []
    if len(rows) < 22:
        return None

    # Bybit returns newest first. Row 0 is the live candle. Rows 1+ are completed.
    done = rows[1:]
    close_1h = float(done[3][4])
    close_4h = float(done[15][4])
    ret_1h = (last / close_1h - 1.0) * 100.0
    ret_4h = (last / close_4h - 1.0) * 100.0

    recent_turnover = sum(float(row[6]) for row in done[:4])
    prior_turnover = sum(float(row[6]) for row in done[4:8])
    volume_ratio = recent_turnover / prior_turnover if prior_turnover > 0 else 0.0

    prior_high = max(float(row[2]) for row in done[:16])
    breakout_pct = (last / prior_high - 1.0) * 100.0
    return {
        "ret_1h_pct": ret_1h,
        "ret_4h_pct": ret_4h,
        "volume_ratio": volume_ratio,
        "prior_4h_high": prior_high,
        "breakout_pct": breakout_pct,
    }


def select_candidate(base_url: str) -> dict | None:
    instruments = get_instruments(base_url)
    result = public_get(base_url, "/v5/market/tickers", {"category": "spot"})
    preliminary = []
    for row in result.get("list") or []:
        symbol = str(row.get("symbol") or "")
        inst = instruments.get(symbol)
        if not inst:
            continue
        base = str(inst.get("baseCoin") or "")
        if base in EXCLUDED_BASES:
            continue
        lot = inst.get("lotSizeFilter") or {}
        min_amt = float(lot.get("minOrderAmt") or 0.0)
        if min_amt <= 0 or min_amt > MAX_NOTIONAL_USDT:
            continue
        last = float(row.get("lastPrice") or 0.0)
        bid = float(row.get("bid1Price") or 0.0)
        ask = float(row.get("ask1Price") or 0.0)
        turnover = float(row.get("turnover24h") or 0.0)
        change_24h = float(row.get("price24hPcnt") or 0.0) * 100.0
        if min(last, bid, ask) <= 0 or turnover < 5_000_000:
            continue
        spread_pct = (ask - bid) / ask * 100.0
        if spread_pct > 0.12 or change_24h < -4.0 or change_24h > 35.0:
            continue
        preliminary.append({
            "symbol": symbol,
            "instrument": inst,
            "last": last,
            "bid": bid,
            "ask": ask,
            "turnover_24h": turnover,
            "change_24h_pct": change_24h,
            "spread_pct": spread_pct,
        })

    # Prefer liquid movers, then validate price path and volume on up to 45 names.
    preliminary.sort(
        key=lambda x: (min(max(x["change_24h_pct"], 0.0), 15.0) + math.log10(x["turnover_24h"]), x["turnover_24h"]),
        reverse=True,
    )
    validated = []
    for item in preliminary[:45]:
        try:
            metrics = kline_metrics(base_url, item["symbol"], item["last"])
        except Exception as exc:
            emit("KLINE_SKIP", symbol=item["symbol"], error=repr(exc)[:180])
            continue
        if not metrics:
            continue
        item.update(metrics)

        # Entry requires positive short-term momentum, expanding volume, and a
        # fresh 4h breakout with limited extension. Extreme candles are rejected.
        if not (0.6 <= item["ret_1h_pct"] <= 7.0):
            continue
        if not (1.0 <= item["ret_4h_pct"] <= 16.0):
            continue
        if item["volume_ratio"] < 1.15:
            continue
        if not (0.0 <= item["breakout_pct"] <= 1.5):
            continue

        score = (
            min(item["ret_1h_pct"], 4.0) * 1.8
            + min(item["ret_4h_pct"], 10.0) * 0.7
            + min(item["volume_ratio"], 3.0) * 2.0
            + min(math.log10(item["turnover_24h"]), 10.0)
            - item["spread_pct"] * 40.0
            - item["breakout_pct"] * 1.5
        )
        item["score"] = score
        validated.append(item)

    if not validated:
        emit("NO_SETUP", scanned=len(preliminary), max_notional_usdt=MAX_NOTIONAL_USDT)
        return None
    validated.sort(key=lambda x: x["score"], reverse=True)
    chosen = validated[0]
    emit(
        "CANDIDATE",
        symbol=chosen["symbol"],
        last=chosen["last"],
        spread_pct=round(chosen["spread_pct"], 5),
        turnover_24h=round(chosen["turnover_24h"], 2),
        change_24h_pct=round(chosen["change_24h_pct"], 3),
        ret_1h_pct=round(chosen["ret_1h_pct"], 3),
        ret_4h_pct=round(chosen["ret_4h_pct"], 3),
        volume_ratio=round(chosen["volume_ratio"], 3),
        breakout_pct=round(chosen["breakout_pct"], 3),
        score=round(chosen["score"], 3),
    )
    return chosen


def balances(base_url: str) -> tuple[float, float]:
    wallet = signed_get(
        base_url,
        "/v5/account/wallet-balance",
        {"accountType": "UNIFIED", "coin": "USDT"},
    ).get("result") or {}
    unified = 0.0
    accounts = wallet.get("list") or []
    if accounts:
        for row in accounts[0].get("coin") or []:
            if row.get("coin") == "USDT":
                unified = float(row.get("walletBalance") or 0.0)

    funding_result = signed_get(
        base_url,
        "/v5/asset/transfer/query-account-coins-balance",
        {"accountType": "FUND", "coin": "USDT"},
    ).get("result") or {}
    funding = 0.0
    for row in funding_result.get("balance") or []:
        if row.get("coin") == "USDT":
            funding = float(row.get("transferBalance") or row.get("walletBalance") or 0.0)
    return unified, funding


def ensure_trade_balance(base_url: str, minimum: float) -> float:
    unified, funding = balances(base_url)
    emit("BALANCE", unified_usdt=unified, funding_usdt=funding)
    if unified + 1e-9 >= minimum:
        return unified
    needed = min(MAX_NOTIONAL_USDT - unified, funding)
    if needed <= 0 or unified + needed + 1e-9 < minimum:
        raise RuntimeError("insufficient_total_usdt")
    transfer_id = str(uuid.uuid4())
    _signed_post(
        base_url,
        "/v5/asset/transfer/inter-transfer",
        {
            "transferId": transfer_id,
            "coin": "USDT",
            "amount": format(needed, ".8f").rstrip("0").rstrip("."),
            "fromAccountType": "FUND",
            "toAccountType": "UNIFIED",
        },
    )
    time.sleep(1.0)
    unified_after, funding_after = balances(base_url)
    emit(
        "TRANSFER",
        transfer_id=transfer_id,
        amount_usdt=needed,
        unified_after_usdt=unified_after,
        funding_after_usdt=funding_after,
    )
    if unified_after + 1e-9 < minimum:
        raise RuntimeError("internal_transfer_not_available")
    return unified_after


def find_existing(base_url: str) -> dict | None:
    for path in ("/v5/order/realtime", "/v5/order/history"):
        result = signed_get(
            base_url,
            path,
            {"category": "spot", "orderLinkId": ORDER_LINK_ID, "limit": 1},
        ).get("result") or {}
        rows = result.get("list") or []
        if rows:
            return rows[0]
    return None


def execute(base_url: str, candidate: dict) -> dict:
    existing = find_existing(base_url)
    if existing:
        emit(
            "EXISTING_ORDER",
            order_id=existing.get("orderId"),
            status=existing.get("orderStatus"),
            symbol=existing.get("symbol"),
            cum_exec_qty=existing.get("cumExecQty"),
            avg_price=existing.get("avgPrice"),
        )
        return existing

    inst = candidate["instrument"]
    lot = inst.get("lotSizeFilter") or {}
    price_filter = inst.get("priceFilter") or {}
    min_amt = float(lot.get("minOrderAmt") or 0.0)
    base_precision = str(lot.get("basePrecision") or "0.00000001")
    tick_size = str(price_filter.get("tickSize") or "0.00000001")
    balance = ensure_trade_balance(base_url, min_amt)
    max_spend = min(MAX_NOTIONAL_USDT, balance)

    ticker_rows = public_get(
        base_url,
        "/v5/market/tickers",
        {"category": "spot", "symbol": candidate["symbol"]},
    ).get("list") or []
    if not ticker_rows:
        raise RuntimeError("ticker_missing_before_order")
    ticker = ticker_rows[0]
    ask = float(ticker.get("ask1Price") or 0.0)
    bid = float(ticker.get("bid1Price") or 0.0)
    spread_pct = (ask - bid) / ask * 100.0 if ask > 0 else 999.0
    if ask <= 0 or bid <= 0 or spread_pct > 0.15:
        raise RuntimeError("spread_or_book_blocked_before_order")
    extension_pct = (ask / candidate["last"] - 1.0) * 100.0
    if extension_pct > 1.0:
        raise RuntimeError("price_extended_before_order")

    limit_price = ceil_step(ask * 1.0004, tick_size)
    quantity = floor_step(max_spend / limit_price, base_precision)
    actual_notional = quantity * limit_price
    if actual_notional + 1e-9 < min_amt:
        raise RuntimeError("minimum_order_exceeds_available_balance")
    if actual_notional > MAX_NOTIONAL_USDT + 1e-8:
        raise RuntimeError("notional_cap_violation")

    stop_price = floor_step(limit_price * 0.97, tick_size)
    take_profit_price = ceil_step(limit_price * 1.06, tick_size)
    payload = {
        "category": "spot",
        "symbol": candidate["symbol"],
        "side": "Buy",
        "orderType": "Limit",
        "qty": format(quantity, ".16g"),
        "price": format(limit_price, ".16g"),
        "timeInForce": "IOC",
        "orderLinkId": ORDER_LINK_ID,
        "isLeverage": 0,
        "orderFilter": "Order",
        "takeProfit": format(take_profit_price, ".16g"),
        "stopLoss": format(stop_price, ".16g"),
        "tpOrderType": "Market",
        "slOrderType": "Market",
    }
    ack = _signed_post(base_url, "/v5/order/create", payload)
    order_id = (ack.get("result") or {}).get("orderId")
    time.sleep(1.5)
    order_result = signed_get(
        base_url,
        "/v5/order/realtime",
        {"category": "spot", "symbol": candidate["symbol"], "orderId": order_id},
    ).get("result") or {}
    rows = order_result.get("list") or []
    order = rows[0] if rows else {}
    execution_result = signed_get(
        base_url,
        "/v5/execution/list",
        {"category": "spot", "symbol": candidate["symbol"], "orderId": order_id, "limit": 20},
    ).get("result") or {}
    executions = execution_result.get("list") or []
    emit(
        "ORDER_RESULT",
        order_id=order_id,
        order_link_id=ORDER_LINK_ID,
        symbol=candidate["symbol"],
        status=order.get("orderStatus"),
        cum_exec_qty=order.get("cumExecQty"),
        avg_price=order.get("avgPrice"),
        limit_price=limit_price,
        quantity=quantity,
        notional_usdt=actual_notional,
        stop_price=stop_price,
        take_profit_price=take_profit_price,
        executions=[
            {
                "exec_id": row.get("execId"),
                "exec_qty": row.get("execQty"),
                "exec_price": row.get("execPrice"),
                "exec_fee": row.get("execFee"),
            }
            for row in executions
        ],
    )
    return order


def main() -> None:
    diagnostic = account_diagnostic()
    emit(
        "ACCOUNT",
        connected=diagnostic.get("connected"),
        read_only=diagnostic.get("read_only"),
        safe_permissions=diagnostic.get("safe_permissions"),
        spot_trade_enabled=diagnostic.get("spot_trade_enabled"),
        forbidden_permissions=diagnostic.get("forbidden_permissions"),
        total_equity_usd=diagnostic.get("total_equity_usd"),
    )
    if diagnostic.get("read_only"):
        raise RuntimeError("api_key_is_read_only")
    forbidden = diagnostic.get("forbidden_permissions") or {}
    if forbidden.get("withdraw"):
        raise RuntimeError("withdraw_permission_present")
    if forbidden.get("contract_trade") or forbidden.get("options"):
        emit(
            "PERMISSION_WARNING",
            note="derivative permissions present but this run is hard-coded to spot and isLeverage=0",
        )
    if not diagnostic.get("spot_trade_enabled"):
        raise RuntimeError("spot_trade_permission_missing")

    base_url, _api_info, _attempts = _find_authenticated_base_url()
    existing = find_existing(base_url)
    if existing:
        emit(
            "EXISTING_ORDER",
            order_id=existing.get("orderId"),
            status=existing.get("orderStatus"),
            symbol=existing.get("symbol"),
            cum_exec_qty=existing.get("cumExecQty"),
            avg_price=existing.get("avgPrice"),
        )
        return
    candidate = select_candidate(base_url)
    if candidate is None:
        return
    execute(base_url, candidate)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit("ERROR", error=_safe_error(exc))
        raise SystemExit(1)
