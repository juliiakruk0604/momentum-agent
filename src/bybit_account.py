from __future__ import annotations

import hashlib
import hmac
import os
import time
from urllib.parse import urlencode

import requests


BASE_URL = os.getenv("BYBIT_BASE_URL", "https://api.bybit.com")
RECV_WINDOW = "5000"


def _credentials() -> tuple[str, str]:
    api_key = os.getenv("BYBIT_API_KEY", "").strip()
    api_secret = os.getenv("BYBIT_API_SECRET", "").strip()
    if not api_key or not api_secret:
        raise RuntimeError("BYBIT_API_KEY/BYBIT_API_SECRET are not configured")
    return api_key, api_secret


def _signed_get(path: str, params: dict | None = None) -> dict:
    api_key, api_secret = _credentials()
    params = params or {}
    query = urlencode(sorted((k, str(v)) for k, v in params.items() if v is not None))
    timestamp = str(int(time.time() * 1000))
    payload = f"{timestamp}{api_key}{RECV_WINDOW}{query}"
    signature = hmac.new(
        api_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    headers = {
        "X-BAPI-API-KEY": api_key,
        "X-BAPI-TIMESTAMP": timestamp,
        "X-BAPI-RECV-WINDOW": RECV_WINDOW,
        "X-BAPI-SIGN": signature,
    }

    url = f"{BASE_URL}{path}"
    response = requests.get(url, params=params, headers=headers, timeout=10)
    response.raise_for_status()
    data = response.json()

    if int(data.get("retCode", -1)) != 0:
        raise RuntimeError(f"Bybit error {data.get('retCode')}: {data.get('retMsg')}")

    return data


def account_diagnostic() -> dict:
    api_info = _signed_get("/v5/user/query-api").get("result") or {}
    wallet = _signed_get(
        "/v5/account/wallet-balance",
        {"accountType": "UNIFIED"},
    ).get("result") or {}

    permissions = api_info.get("permissions") or {}
    wallet_permissions = set(permissions.get("Wallet") or [])
    contract_permissions = set(permissions.get("ContractTrade") or [])
    options_permissions = set(permissions.get("Options") or [])
    spot_permissions = set(permissions.get("Spot") or [])

    forbidden = {
        "withdraw": "Withdraw" in wallet_permissions,
        "contract_trade": bool(contract_permissions),
        "options": bool(options_permissions),
    }
    safe_permissions = not any(forbidden.values())

    accounts = wallet.get("list") or []
    account = accounts[0] if accounts else {}
    coins = []
    for row in account.get("coin") or []:
        coins.append(
            {
                "coin": row.get("coin"),
                "wallet_balance": row.get("walletBalance"),
                "equity": row.get("equity"),
                "usd_value": row.get("usdValue"),
            }
        )

    return {
        "connected": True,
        "safe_permissions": safe_permissions,
        "read_only": int(api_info.get("readOnly", 0)) == 1,
        "spot_trade_enabled": "SpotTrade" in spot_permissions,
        "forbidden_permissions": forbidden,
        "deadline_days": api_info.get("deadlineDay"),
        "account_type": account.get("accountType"),
        "total_equity_usd": account.get("totalEquity"),
        "total_available_balance_usd": account.get("totalAvailableBalance"),
        "coins": coins,
        "execution_enabled": False,
    }
