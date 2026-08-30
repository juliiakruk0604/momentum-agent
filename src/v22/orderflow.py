from __future__ import annotations


def book_features(book):
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    if not bids or not asks:
        return {
            "ok": False,
            "book_imbalance": 0.0,
            "spread_pct": None,
            "depth_usdt": 0.0,
        }
    bid, ask = float(bids[0][0]), float(asks[0][0])
    spread = (ask / bid - 1.0) * 100.0 if bid > 0 else 999.0

    bid5 = sum(float(p) * float(q) for p, q in bids[:5])
    ask5 = sum(float(p) * float(q) for p, q in asks[:5])
    total = bid5 + ask5
    imbalance = 0.0 if total <= 0 else (bid5 - ask5) / total

    return {
        "ok": True,
        "best_bid": bid,
        "best_ask": ask,
        "spread_pct": spread,
        "book_imbalance": imbalance,
        "depth_usdt": min(bid5, ask5),
    }


def trade_flow_features(trades):
    if not trades:
        return {
            "ok": False,
            "buy_ratio": 0.5,
            "signed_notional_ratio": 0.0,
            "trade_intensity_per_sec": 0.0,
            "observed_seconds": 0.0,
            "trade_count": 0,
        }

    rows = sorted(trades, key=lambda x: int(x.get("time") or 0))
    buy = 0.0
    sell = 0.0
    for t in rows:
        notional = float(t.get("price") or 0.0) * float(t.get("size") or 0.0)
        if str(t.get("side") or "").lower() == "buy":
            buy += notional
        else:
            sell += notional

    total = buy + sell
    t0 = int(rows[0].get("time") or 0)
    t1 = int(rows[-1].get("time") or t0)
    seconds = max((t1 - t0) / 1000.0, 1.0)

    recent_cutoff = t1 - 30_000
    recent_rows = [t for t in rows if int(t.get("time") or 0) >= recent_cutoff]
    recent_buy = 0.0
    recent_sell = 0.0
    for t in recent_rows:
        notional = float(t.get("price") or 0.0) * float(t.get("size") or 0.0)
        if str(t.get("side") or "").lower() == "buy":
            recent_buy += notional
        else:
            recent_sell += notional
    recent_total = recent_buy + recent_sell
    recent_seconds = max(
        (t1 - int(recent_rows[0].get("time") or t1)) / 1000.0,
        1.0,
    ) if recent_rows else 1.0

    return {
        "ok": True,
        "buy_ratio": 0.5 if total <= 0 else buy / total,
        "signed_notional_ratio": 0.0 if total <= 0 else (buy - sell) / total,
        "trade_intensity_per_sec": len(rows) / seconds,
        "observed_seconds": seconds,
        "trade_count": len(rows),
        "buy_notional": buy,
        "sell_notional": sell,
        "recent_buy_ratio": 0.5 if recent_total <= 0 else recent_buy / recent_total,
        "recent_signed_notional_ratio": 0.0 if recent_total <= 0 else (recent_buy - recent_sell) / recent_total,
        "recent_trade_count": len(recent_rows),
        "recent_notional": recent_total,
        "recent_trade_intensity_per_sec": len(recent_rows) / recent_seconds,
    }


def flow_score(book, trades):
    b = book_features(book)
    t = trade_flow_features(trades)
    score = 0.0
    if b.get("ok"):
        score += 35.0 * min(max((float(b["book_imbalance"]) + 0.15) / 0.50, 0.0), 1.0)
        if float(b.get("spread_pct") or 999.0) <= 0.08:
            score += 10.0
    if t.get("ok"):
        buy_ratio = float(t.get("recent_buy_ratio", t["buy_ratio"]))
        signed_ratio = float(t.get("recent_signed_notional_ratio", t["signed_notional_ratio"]))
        score += 35.0 * min(max((buy_ratio - 0.50) / 0.25, 0.0), 1.0)
        score += 20.0 * min(max(signed_ratio, 0.0) / 0.35, 1.0)
    return round(max(0.0, min(100.0, score)), 2), b, t
