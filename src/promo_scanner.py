from __future__ import annotations

import html
import re
import time
from dataclasses import dataclass, asdict
from typing import Any

import requests


BASE_URL = "https://api.bybit.com"
SAFE_KEYWORDS = (
    "reward", "airdrop", "earn", "hold", "deposit", "campaign",
    "bonus", "launchpool", "token splash", "cashback",
)
RISKY_KEYWORDS = (
    "futures", "perpetual", "derivative", "margin", "options",
    "leverage", "copy trading", "perps", "tradfi",
)
MANUAL_KEYWORDS = (
    "invite", "referral", "refer", "lottery", "lucky draw",
    "randomly selected", "first come", "fcfs", "connect wallet",
)
REGION_KEYWORDS = (
    "exclusive", "eligible regions", "selected regions", "restricted jurisdictions",
)
NEW_USER_KEYWORDS = (
    "new users only", "new user only", "first deposit", "first trade",
    "newly registered", "new users",
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
    min_capital_usd: float | None = None
    reward_summary: str | None = None
    requires_new_user: bool = False
    region_restricted: bool = False
    manual_or_lottery: bool = False
    detail_checked: bool = False

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


def _page_text(url: str) -> str:
    if not url:
        return ""
    response = requests.get(
        url,
        timeout=10,
        headers={"User-Agent": "Mozilla/5.0 promo-research-agent/1.0"},
    )
    response.raise_for_status()
    text = response.text
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _extract_min_capital(text: str) -> float | None:
    patterns = [
        r"(?:minimum(?: of)?|min\.?|at least|hold at least|hold a minimum of|deposit at least|minimum deposit(?: of)?|minimum holding(?: of)?)\s*[:\-]?\s*(\d+(?:\.\d+)?)\s*(?:USDT|USDC|USD1|USD)",
        r"(\d+(?:\.\d+)?)\s*(?:USDT|USDC|USD1|USD)\s*(?:minimum|min\.?|or more|and above)",
    ]
    values: list[float] = []
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.I):
            try:
                values.append(float(match))
            except Exception:
                pass
    return min(values) if values else None


def _extract_reward_summary(text: str) -> str | None:
    candidates = re.findall(
        r"((?:earn|receive|get|reward(?:ed)? with|up to)\s+[^.]{0,90}?(?:USDT|USDC|USD1|WLFI|bonus|reward))",
        text,
        flags=re.I,
    )
    if not candidates:
        return None
    cleaned = re.sub(r"\s+", " ", candidates[0]).strip()
    return cleaned[:180]


def _enrich(candidate: PromoCandidate) -> PromoCandidate:
    try:
        text = _page_text(candidate.url)
    except Exception:
        candidate.reasons.append("detail_fetch_failed")
        return candidate

    lower = text.lower()
    candidate.detail_checked = True
    candidate.min_capital_usd = _extract_min_capital(text)
    candidate.reward_summary = _extract_reward_summary(text)
    candidate.requires_new_user = any(k in lower for k in NEW_USER_KEYWORDS)
    candidate.region_restricted = any(k in lower for k in REGION_KEYWORDS)
    candidate.manual_or_lottery = any(k in lower for k in MANUAL_KEYWORDS)

    if candidate.min_capital_usd is not None:
        candidate.reasons.append(f"min_capital_detected:{candidate.min_capital_usd:g}")
        if candidate.min_capital_usd <= 2:
            candidate.score += 20
        elif candidate.min_capital_usd <= 15:
            candidate.score += 8
        else:
            candidate.score -= 20

    if candidate.requires_new_user:
        candidate.reasons.append("new_user_requirement")
        candidate.score -= 15

    if candidate.region_restricted:
        candidate.reasons.append("region_check_required")
        candidate.score -= 10

    if candidate.manual_or_lottery:
        candidate.reasons.append("manual_or_probability_component")
        candidate.score -= 15

    # Risk classification comes from the announcement metadata/title.
    # Full page HTML includes Bybit navigation with unrelated Futures/Options text.
    risky_detail = []

    # AUTO here means eligible for future safe automation.
    # It does not execute anything. Execution remains hard-disabled elsewhere.
    if (
        candidate.detail_checked
        and candidate.min_capital_usd is not None
        and candidate.min_capital_usd <= 2
        and not candidate.requires_new_user
        and not candidate.region_restricted
        and not candidate.manual_or_lottery
        and candidate.score >= 50
    ):
        candidate.action = "AUTO"
    elif candidate.score >= 30:
        candidate.action = "APPROVAL"
    else:
        candidate.action = "WATCH"

    return candidate


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

    title_numbers = [
        float(x.replace(",", ""))
        for x in re.findall(r"(\d[\d,]*(?:\.\d+)?)\s*USDT", title, flags=re.I)
    ]
    if title_numbers and min(title_numbers) <= 20:
        score += 10
        reasons.append("small_usdt_amount_in_title")

    action = "WATCH"
    if score >= 40 and not risky_hits:
        action = "APPROVAL"
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


def scan_promos(limit: int = 50, enrich_top: int = 8) -> dict:
    now_ms = int(time.time() * 1000)
    rows = _fetch_announcements(limit=limit)
    candidates = [_score(row, now_ms) for row in rows]
    candidates.sort(key=lambda x: (x.score, x.published_at_ms or 0), reverse=True)

    for index, candidate in enumerate(candidates[:max(0, enrich_top)]):
        candidates[index] = _enrich(candidate)

    candidates.sort(key=lambda x: (x.score, x.published_at_ms or 0), reverse=True)
    return {
        "source": "bybit_v5_announcements",
        "scanned": len(rows),
        "generated_at_ms": now_ms,
        "execution_enabled": False,
        "candidates": [x.to_dict() for x in candidates[:20]],
    }
