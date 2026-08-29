import api


class FakeStore:
    def __init__(self, historical_status):
        self._historical_status = historical_status

    def micro_live_readiness(self):
        return {
            "ready": True,
            "reasons": [],
            "historical": self._historical_status,
            "eligible_candidates": [{"symbol": "TESTUSDT"}],
        }

    def historical_status(self):
        return self._historical_status


def test_micro_live_requires_full_historical_research_gate(monkeypatch):
    historical = {
        "status": "complete",
        "progress": {"quality": [{"status": "ok", "n": 733}]},
        "oos": {
            "research_gate": {
                "passed": False,
                "reasons": ["need_at_least_30_oos_confirmed"],
            }
        },
    }
    monkeypatch.setattr(api, "store", FakeStore(historical))

    readiness = api._micro_live_readiness()

    assert readiness["ready"] is False
    assert "historical_research_gate_not_passed" in readiness["reasons"]
    assert readiness["historical"]["oos"]["research_gate"]["passed"] is False
