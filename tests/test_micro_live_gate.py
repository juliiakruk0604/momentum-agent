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


class LegacyPartialStore(FakeStore):
    def _execute(self, sql_sqlite, sql_pg, params=(), fetch=None):
        assert "exact5_errors" in sql_sqlite
        assert "exact5_errors" in sql_pg
        assert params == (self._historical_status["dataset_id"],)
        assert fetch == "one"
        return {"n": 1}


def test_micro_live_blocked_while_historical_backfill_running(monkeypatch):
    monkeypatch.setattr(api, "store", FakeStore({"status": "running", "progress": {"quality": []}}))

    readiness = api._micro_live_readiness()

    assert readiness["ready"] is False
    assert "historical_backfill_not_complete" in readiness["reasons"]


def test_micro_live_blocked_when_completed_backfill_has_errors(monkeypatch):
    historical = {
        "status": "complete",
        "progress": {
            "quality": [
                {"status": "ok", "n": 730},
                {"status": "empty", "n": 2},
                {"status": "error", "n": 1},
            ]
        },
    }
    monkeypatch.setattr(api, "store", FakeStore(historical))

    readiness = api._micro_live_readiness()

    assert readiness["ready"] is False
    assert readiness["historical_backfill_error_count"] == 1
    assert "historical_backfill_has_errors" in readiness["reasons"]


def test_micro_live_blocked_when_completed_backfill_has_partial_runs(monkeypatch):
    historical = {
        "status": "complete",
        "progress": {
            "quality": [
                {"status": "ok", "n": 730},
                {"status": "empty", "n": 2},
                {"status": "partial", "n": 1},
            ]
        },
    }
    monkeypatch.setattr(api, "store", FakeStore(historical))

    readiness = api._micro_live_readiness()

    assert readiness["ready"] is False
    assert readiness["historical_backfill_partial_run_count"] == 1
    assert "historical_backfill_has_partial_runs" in readiness["reasons"]


def test_micro_live_blocked_on_legacy_exact5_partial_run(monkeypatch):
    historical = {
        "status": "complete",
        "dataset_id": "legacy-oos",
        "progress": {
            "quality": [
                {"status": "ok", "n": 731},
                {"status": "empty", "n": 2},
            ]
        },
    }
    monkeypatch.setattr(api, "store", LegacyPartialStore(historical))

    readiness = api._micro_live_readiness()

    assert readiness["ready"] is False
    assert readiness["historical_backfill_partial_run_count"] == 1
    assert readiness["historical_backfill_legacy_partial_run_count"] == 1
    assert "historical_backfill_has_partial_runs" in readiness["reasons"]


def test_micro_live_preserves_ready_after_clean_historical_backfill_complete(monkeypatch):
    historical = {
        "status": "complete",
        "progress": {
            "quality": [
                {"status": "ok", "n": 731},
                {"status": "empty", "n": 2},
            ]
        },
    }
    monkeypatch.setattr(api, "store", FakeStore(historical))

    readiness = api._micro_live_readiness()

    assert readiness["ready"] is True
    assert readiness["historical_backfill_error_count"] == 0
    assert readiness["historical_backfill_partial_run_count"] == 0
    assert readiness["historical_backfill_legacy_partial_run_count"] == 0
    assert "historical_backfill_not_complete" not in readiness["reasons"]
    assert "historical_backfill_has_errors" not in readiness["reasons"]
    assert "historical_backfill_has_partial_runs" not in readiness["reasons"]
