from __future__ import annotations

import re
import time
from dataclasses import dataclass, asdict
from typing import Any

import requests


BASE_URL = "https://api.bybit.com"
SAFE_KEYWORDS = (
    "reward", "airdrop", "earn", "hold", "deposit", "campaign",
    "bonus", "launchpool", "token splash", "cashback", "share",
)
RISKY_KEYWORDS = (
    "futures", "perpetual", "derivative", "margin", "options",
    "leverage", "copy trading", "perps", "tradfi",
)


@dataclass
class PromoCandidate:
    title: str
    url: str
    published_at_ms: int | None
    starts_at_ms: int | None
    ends_at_ms: int | None
    tags: list[str]
    score: int
    action: str
    reasons: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


def _fetch_announcements(limit: int = 50) -> list[dict[str, Any]]:
    response = requests.get(
        f"{BASE_URL}/v5/announcements/index",
        params={"locale": "en-US", "limit": max(1, min(limit, 50))},
        timeout=10,
    )
    response.raise_for_status()
    data = response.json()
    if int(data.get("retCode", -1)) != 0:
        raise RuntimeError(f"Bybit announcements error {data.get('retCode')}: {data.get('retMsg')}")
    return ((data.get("result") or {}).get("list") or [])


def _score(row: dict[str, Any], now_ms: int) -> PromoCandidate:
    title = str(row.get("title") or "")
    description = str(row.get("description") or "")
    tags = [str(x) for x in (row.get("tags") or [])]
    haystack = " ".join([title, description, *tags]).lower()

    score = 0
    reasons: list[str] = []

    safe_hits = [k for k in SAFE_KEYWORDS if k in haystack]
    risky_hits = [k for k in RISKY_KEYWORDS if k in haystack]

    if safe_hits:
        score += min(35, 8 * len(safe_hits))
        reasons.append("reward_or_earn_language")

    if "deposit" in haystack:
        score += 8
        reasons.append("deposit_task_possible")

    if "hold" in haystack or "earn" in haystack:
        score += 10
        reasons.append("non_trading_path_possible")

    if "hold & earn" in haystack or ("hold" in haystack and "earn" in haystack):
        score += 20
        reasons.append("hold_and_earn_preferred")

    if "airdrop" in haystack or "bonus" in haystack or "cashback" in haystack:
        score += 12
        reasons.append("direct_reward_language")

    if risky_hits:
        score -= 50
        reasons.append("leveraged_or_derivatives_language")

    end_ms = row.get("endDataTimestamp")
    if end_ms:
        try:
            if int(end_ms) < now_ms:
                score -= 100
                reasons.append("expired")
        except Exception:
            pass

    title_numbers = [float(x.replace(",", "")) for x in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*USDT", title, flags=re.I)]
    if title_numbers and min(title_numbers) <= 20:
        score += 10
        reasons.append("small_usdt_amount_in_title")

    action = "WATCH"
    if score >= 40 and not risky_hits:
        action = "REVIEW"
    if score < 0:
        action = "REJECT"

    return PromoCandidate(
        title=title,
        url=str(row.get("url") or ""),
        published_at_ms=row.get("publishTime"),
        starts_at_ms=row.get("startDataTimestamp"),
        ends_at_ms=row.get("endDataTimestamp"),
        tags=tags,
        score=score,
        action=action,
        reasons=reasons,
    )


def scan_promos(limit: int = 50) -> dict:
    now_ms = int(time.time() * 1000)
    rows = _fetch_announcements(limit=limit)
    candidates = [_score(row, now_ms) for row in rows]
    candidates.sort(key=lambda x: (x.score, x.published_at_ms or 0), reverse=True)
    return {
        "source": "bybit_v5_announcements",
        "scanned": len(rows),
        "generated_at_ms": now_ms,
        "candidates": [x.to_dict() for x in candidates[:20]],
    }
