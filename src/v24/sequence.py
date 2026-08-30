from __future__ import annotations

from collections import defaultdict, deque
import time


class SequenceFeatureEngine:
    def __init__(self, max_points=20):
        self.history = defaultdict(lambda: deque(maxlen=int(max_points)))

    @staticmethod
    def _f(value, default=0.0):
        try:
            return float(value)
        except Exception:
            return float(default)

    def enrich(self, feature: dict, now_ms=None):
        now_ms = int(now_ms or time.time() * 1000)
        symbol = str(feature.get("symbol") or "")
        if not symbol:
            return feature

        row = {
            "ts_ms": now_ms,
            "microstructure_score": self._f(feature.get("microstructure_score")),
            "book_imbalance_5": self._f(feature.get("book_imbalance_5")),
            "ask_depletion_1s": self._f(feature.get("ask_depletion_1s")),
            "price_move_1s_pct": self._f(feature.get("price_move_1s_pct")),
            "buy_ratio_1s": self._f((feature.get("trade_1s") or {}).get("buy_ratio"), 0.5),
            "flow_confidence_1s": self._f(feature.get("flow_confidence_1s")),
            "external_lead_1s": self._f(
                (feature.get("cross_exchange") or {}).get("external_minus_bybit_move_1s_pct")
            ),
            "external_consensus_1s": self._f(
                (feature.get("cross_exchange") or {}).get("external_consensus_move_1s_pct")
            ),
            "oi_change_5s_pct": self._f(
                (feature.get("perp_context") or {}).get("oi_change_5s_pct")
            ),
        }

        hist = self.history[symbol]
        hist.append(row)

        def prior(seconds):
            target = now_ms - int(seconds * 1000)
            values = [x for x in hist if int(x["ts_ms"]) <= target]
            return values[-1] if values else None

        p1 = prior(1)
        p3 = prior(3)
        recent3 = [x for x in hist if now_ms - int(x["ts_ms"]) <= 3000]
        recent5 = [x for x in hist if now_ms - int(x["ts_ms"]) <= 5000]

        score = row["microstructure_score"]
        score_delta_1s = 0.0 if p1 is None else score - p1["microstructure_score"]
        score_delta_3s = 0.0 if p3 is None else score - p3["microstructure_score"]

        def mean(rows, key, default=0.0):
            if not rows:
                return default
            return sum(float(x[key]) for x in rows) / len(rows)

        score_mean_3s = mean(recent3, "microstructure_score")
        score_min_3s = min((x["microstructure_score"] for x in recent3), default=score)
        buy_mean_3s = mean(recent3, "buy_ratio_1s", 0.5)
        imbalance_mean_3s = mean(recent3, "book_imbalance_5")
        depletion_mean_3s = mean(recent3, "ask_depletion_1s")
        lead_mean_3s = mean(recent3, "external_lead_1s")
        consensus_mean_3s = mean(recent3, "external_consensus_1s")
        oi_mean_5s = mean(recent5, "oi_change_5s_pct")

        score_persistence_3s = (
            0.0
            if not recent3
            else sum(x["microstructure_score"] >= 65.0 for x in recent3) / len(recent3)
        )
        buy_persistence_3s = (
            0.0
            if not recent3
            else sum(x["buy_ratio_1s"] >= 0.55 for x in recent3) / len(recent3)
        )
        positive_lead_persistence_3s = (
            0.0
            if not recent3
            else sum(x["external_lead_1s"] > 0.0 for x in recent3) / len(recent3)
        )
        agreement_persistence_3s = (
            0.0
            if not recent3
            else sum(
                (x["external_consensus_1s"] > 0.0 and x["price_move_1s_pct"] >= 0.0)
                or (x["external_consensus_1s"] < 0.0 and x["price_move_1s_pct"] <= 0.0)
                for x in recent3
            ) / len(recent3)
        )

        sequence = {
            "points_3s": len(recent3),
            "points_5s": len(recent5),
            "score_delta_1s": score_delta_1s,
            "score_delta_3s": score_delta_3s,
            "score_mean_3s": score_mean_3s,
            "score_min_3s": score_min_3s,
            "score_persistence_3s": score_persistence_3s,
            "buy_ratio_mean_3s": buy_mean_3s,
            "buy_persistence_3s": buy_persistence_3s,
            "book_imbalance_mean_3s": imbalance_mean_3s,
            "ask_depletion_mean_3s": depletion_mean_3s,
            "external_lead_mean_3s": lead_mean_3s,
            "external_consensus_mean_3s": consensus_mean_3s,
            "external_positive_lead_persistence_3s": positive_lead_persistence_3s,
            "cross_exchange_agreement_persistence_3s": agreement_persistence_3s,
            "oi_change_mean_5s_pct": oi_mean_5s,
            "evidence_only": True,
        }
        return {**feature, "sequence_context": sequence}
