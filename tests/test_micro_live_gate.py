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


def test_micro_live_blocked_while_historical_backfill_running(monkeypatch):
    monkeypatch.setattr(api, "store", FakeStore({"status": "running"}))

    readiness = api._micro_live_readiness()

    assert readiness["ready"] is False
    assert "historical_backfill_not_complete" in readiness["reasons"]


def test_micro_live_preserves_ready_after_historical_backfill_complete(monkeypatch):
    monkeypatch.setattr(api, "store", FakeStore({"status": "complete"}))

    readiness = api._micro_live_readiness()

    assert readiness["ready"] is True
    assert "historical_backfill_not_complete" not in readiness["reasons"]
