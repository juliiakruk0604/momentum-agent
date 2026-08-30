from __future__ import annotations

import math
import random
import statistics


def max_drawdown(equity_curve):
    peak = None
    worst = 0.0
    for value in equity_curve:
        v = float(value)
        peak = v if peak is None else max(peak, v)
        if peak and peak > 0:
            worst = min(worst, (v / peak - 1.0) * 100.0)
    return worst


def trade_metrics(trades, starting_equity=15.0):
    pnls = [float(t.get("pnl_usdt") or t.get("realized_pnl_usdt") or 0.0) for t in trades]
    equity = [float(starting_equity)]
    for pnl in pnls:
        equity.append(equity[-1] + pnl)

    wins = [x for x in pnls if x > 0]
    losses = [x for x in pnls if x < 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))

    mean = statistics.mean(pnls) if pnls else 0.0
    stdev = statistics.pstdev(pnls) if len(pnls) > 1 else 0.0
    sharpe_trade = 0.0 if stdev <= 1e-12 else mean / stdev * math.sqrt(len(pnls))

    max_consecutive_losses = 0
    current_losses = 0
    for pnl in pnls:
        if pnl < 0:
            current_losses += 1
            max_consecutive_losses = max(max_consecutive_losses, current_losses)
        else:
            current_losses = 0

    return {
        "n": len(pnls),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": 0.0 if not pnls else len(wins) / len(pnls) * 100.0,
        "expectancy_usdt": mean,
        "profit_factor": None if gross_loss <= 1e-12 else gross_win / gross_loss,
        "net_pnl_usdt": sum(pnls),
        "ending_equity_usdt": equity[-1],
        "max_drawdown_pct": max_drawdown(equity),
        "trade_sharpe": sharpe_trade,
        "max_consecutive_losses": max_consecutive_losses,
        "equity_curve": equity,
    }


def monte_carlo(trades, starting_equity=15.0, simulations=2000, seed=42):
    pnls = [float(t.get("pnl_usdt") or t.get("realized_pnl_usdt") or 0.0) for t in trades]
    if not pnls:
        return {"simulations": 0, "reason": "no_trades"}

    rng = random.Random(seed)
    endings, drawdowns = [], []
    for _ in range(int(simulations)):
        sample = [rng.choice(pnls) for _ in pnls]
        equity = [float(starting_equity)]
        for pnl in sample:
            equity.append(equity[-1] + pnl)
        endings.append(equity[-1])
        drawdowns.append(max_drawdown(equity))

    endings.sort()
    drawdowns.sort()
    def q(values, p):
        if not values:
            return None
        idx = min(len(values)-1, max(0, int(round((len(values)-1)*p))))
        return values[idx]

    return {
        "simulations": int(simulations),
        "ending_equity_p05": q(endings, 0.05),
        "ending_equity_p50": q(endings, 0.50),
        "ending_equity_p95": q(endings, 0.95),
        "max_drawdown_p05": q(drawdowns, 0.05),
        "max_drawdown_p50": q(drawdowns, 0.50),
        "max_drawdown_p95": q(drawdowns, 0.95),
        "probability_finish_below_start": sum(x < float(starting_equity) for x in endings) / len(endings),
    }


def grouped_trade_metrics(trades, key, starting_equity=15.0):
    groups = {}
    for trade in trades:
        value = str(trade.get(key) or "UNKNOWN")
        groups.setdefault(value, []).append(trade)
    out = {}
    for value, subset in groups.items():
        metrics = trade_metrics(subset, starting_equity=starting_equity)
        out[value] = {k: v for k, v in metrics.items() if k != "equity_curve"}
    return out
