#!/usr/bin/env python3
"""Watch PALU and BNBHOLDER for confirmed 4h breakout/retest setups.

The script is intentionally read-only with respect to exchanges. It reads public
DEX Screener and GeckoTerminal endpoints, then updates local JSON files only
when a new setup is confirmed.
"""

from __future__ import annotations

import argparse
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


DEXSCREENER_TOKEN_URL = "https://api.dexscreener.com/latest/dex/tokens/{address}"
GECKOTERMINAL_OHLCV_URL = (
    "https://api.geckoterminal.com/api/v2/networks/bsc/pools/{pool}/"
    "ohlcv/hour?aggregate=4&limit=8&currency=usd&token={token_side}"
)
FOUR_HOURS = 4 * 60 * 60


@dataclass(frozen=True)
class TokenConfig:
    symbol: str
    address: str
    breakout: float
    retest_low: float
    retest_high: float
    invalidation: float
    targets: tuple[float, ...]


TOKENS = (
    TokenConfig(
        symbol="PALU",
        address="0x02e75d28a8aa2a0033b8cf866fcf0bb0e1ee4444",
        breakout=0.00130,
        retest_low=0.00127,
        retest_high=0.00130,
        invalidation=0.00118,
        targets=(0.00147, 0.00175, 0.00240),
    ),
    TokenConfig(
        symbol="BNBHOLDER",
        address="0x44440f83419de123d7d411187adb9962db017d03",
        breakout=0.000733,
        retest_low=0.000700,
        retest_high=0.000733,
        invalidation=0.000650,
        targets=(0.000900, 0.001100, 0.001340),
    ),
)


def fetch_json(url: str, attempts: int = 3) -> dict[str, Any]:
    headers = {
        "Accept": "application/json",
        "User-Agent": "momentum-agent-alpha-watch/1.0",
    }
    last_error: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise RuntimeError(f"Unable to fetch {url}: {last_error}")


def select_pool(token: TokenConfig, fetcher: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    payload = fetcher(DEXSCREENER_TOKEN_URL.format(address=token.address))
    candidates = []
    for pair in payload.get("pairs") or []:
        base_address = str((pair.get("baseToken") or {}).get("address", "")).lower()
        quote_address = str((pair.get("quoteToken") or {}).get("address", "")).lower()
        watched = token.address.lower()
        if pair.get("chainId") != "bsc" or watched not in {base_address, quote_address}:
            continue
        liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
        if liquidity <= 0:
            continue
        candidate = dict(pair)
        candidate["_token_side"] = "base" if base_address == watched else "quote"
        candidate["_liquidity_usd"] = liquidity
        candidates.append(candidate)
    if not candidates:
        raise RuntimeError(f"No liquid BSC pool found for {token.symbol}")
    return max(candidates, key=lambda pair: pair["_liquidity_usd"])


def parse_candles(payload: dict[str, Any]) -> list[dict[str, float]]:
    rows = (((payload.get("data") or {}).get("attributes") or {}).get("ohlcv_list") or [])
    candles = []
    for row in rows:
        if not isinstance(row, list) or len(row) < 6:
            continue
        candles.append(
            {
                "timestamp": int(row[0]),
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]),
            }
        )
    return sorted(candles, key=lambda candle: candle["timestamp"])


def evaluate_setup(
    token: TokenConfig,
    candles: list[dict[str, float]],
    current_price: float,
    now: int,
) -> dict[str, Any] | None:
    closed = [candle for candle in candles if candle["timestamp"] + FOUR_HOURS <= now]
    if len(closed) < 2:
        return None

    breakout_candle = closed[-1]
    previous_candle = closed[-2]
    if breakout_candle["close"] <= token.breakout:
        return None
    if breakout_candle["volume"] <= previous_candle["volume"]:
        return None

    later_candles = [
        candle for candle in candles if candle["timestamp"] > breakout_candle["timestamp"]
    ]
    if not later_candles:
        return None
    retest_candle = later_candles[-1]
    retest_low = retest_candle["low"]
    returned_to_zone = token.retest_low <= retest_low <= token.retest_high
    held_zone = current_price >= token.retest_low and retest_candle["close"] >= token.retest_low
    if not returned_to_zone or not held_zone:
        return None

    previous_volume = previous_candle["volume"]
    volume_change_pct = (
        ((breakout_candle["volume"] / previous_volume) - 1) * 100
        if previous_volume > 0
        else None
    )
    return {
        "event_key": f"{token.symbol}:{int(breakout_candle['timestamp'])}",
        "symbol": token.symbol,
        "current_price": current_price,
        "breakout_close": breakout_candle["close"],
        "breakout_candle_open_time": int(breakout_candle["timestamp"]),
        "breakout_volume_usd": breakout_candle["volume"],
        "previous_volume_usd": previous_volume,
        "volume_change_pct": volume_change_pct,
        "retest_candle_low": retest_low,
        "entry_zone": [token.retest_low, token.retest_high],
        "invalidation": token.invalidation,
        "targets": list(token.targets),
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"fired_event_keys": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"fired_event_keys": []}
    if not isinstance(data, dict):
        return {"fired_event_keys": []}
    data.setdefault("fired_event_keys", [])
    return data


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run(
    state_path: Path,
    output_path: Path,
    fetcher: Callable[[str], dict[str, Any]] = fetch_json,
    now: int | None = None,
) -> list[dict[str, Any]]:
    now = int(now if now is not None else time.time())
    state = load_state(state_path)
    fired = set(state.get("fired_event_keys") or [])
    signals: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []

    for token in TOKENS:
        try:
            pair = select_pool(token, fetcher)
            pool_address = str(pair.get("pairAddress") or "")
            token_side = pair["_token_side"]
            ohlcv_url = GECKOTERMINAL_OHLCV_URL.format(
                pool=pool_address, token_side=token_side
            )
            candles = parse_candles(fetcher(ohlcv_url))
            current_price = float(pair.get("priceUsd") or 0)
            signal = evaluate_setup(token, candles, current_price, now)
            if signal and signal["event_key"] not in fired:
                signal.update(
                    {
                        "detected_at": now,
                        "token_address": token.address,
                        "pool_address": pool_address,
                        "liquidity_usd": pair["_liquidity_usd"],
                        "dexscreener_url": pair.get("url"),
                        "geckoterminal_ohlcv_url": ohlcv_url,
                    }
                )
                signals.append(signal)
        except Exception as error:  # Keep one bad market feed from blocking the other token.
            errors.append({"symbol": token.symbol, "error": str(error)})

    if signals:
        new_keys = [signal["event_key"] for signal in signals]
        state["fired_event_keys"] = sorted(fired.union(new_keys))[-100:]
        state["last_signal_at"] = now
        write_json(state_path, state)
        write_json(
            output_path,
            {
                "generated_at": now,
                "signals": signals,
                "feed_errors": errors,
                "execution": "alerts_only_no_orders",
            },
        )
    return signals


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state", default="alpha_alerts/state.json")
    parser.add_argument("--output", default="alpha_alerts/latest.json")
    args = parser.parse_args()
    signals = run(Path(args.state), Path(args.output))
    print(json.dumps({"new_signals": len(signals)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
