from __future__ import annotations

import requests


BYBIT_BASE = "https://api.bybit.com"


def bybit_linear_constraints(symbol: str, timeout: int = 10) -> dict:
    response = requests.get(
        BYBIT_BASE + "/v5/market/instruments-info",
        params={"category": "linear", "symbol": symbol},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if payload.get("retCode") != 0:
        raise RuntimeError(f"{payload.get('retCode')}: {payload.get('retMsg')}")

    rows = (payload.get("result") or {}).get("list") or []
    if not rows:
        raise RuntimeError(f"instrument_not_found:{symbol}")

    row = rows[0]
    lot = row.get("lotSizeFilter") or {}
    price = row.get("priceFilter") or {}

    def f(value):
        if value in (None, ""):
            return None
        return float(value)

    return {
        "symbol": row.get("symbol") or symbol,
        "status": row.get("status"),
        "contract_type": row.get("contractType"),
        "quote_coin": row.get("quoteCoin"),
        "min_notional_usdt": f(lot.get("minNotionalValue")),
        "min_order_qty": f(lot.get("minOrderQty")),
        "max_order_qty": f(lot.get("maxOrderQty")),
        "qty_step": f(lot.get("qtyStep")),
        "tick_size": f(price.get("tickSize")),
    }
