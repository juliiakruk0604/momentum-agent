import service


class FakeStore:
    def __init__(self, unresolved, retry_attempts=None):
        self.unresolved = unresolved
        self.retry_attempts = retry_attempts or {}

    def get_runtime(self, key):
        assert key == "historical_backfill_state"
        return {
            "value": {
                "unresolved_retry_symbols": self.unresolved,
                "retry_attempts": self.retry_attempts,
            }
        }


class FakeWorker:
    def __init__(self):
        self.calls = 0

    def process_micro_live(self, store):
        self.calls += 1
        return {"action": "READY"}


def test_micro_live_integrity_guard_blocks_retryable_historical_runs(monkeypatch):
    monkeypatch.setenv("HISTORICAL_BACKFILL_RETRY_ATTEMPTS", "5")
    worker = FakeWorker()
    service.install_micro_live_integrity_guard(worker)

    result = worker.process_micro_live(
        FakeStore(["BBBUSDT", "AAAUSDT", "AAAUSDT"], {"AAAUSDT": 5, "BBBUSDT": 4})
    )

    assert result == {
        "action": "NO_TRADE",
        "reason": "historical_backfill_has_unresolved_runs",
        "unresolved_count": 2,
        "retryable_count": 1,
        "exhausted_count": 1,
    }
    assert worker.calls == 0


def test_micro_live_integrity_guard_surfaces_exhausted_retry_budget(monkeypatch):
    monkeypatch.setenv("HISTORICAL_BACKFILL_RETRY_ATTEMPTS", "5")
    worker = FakeWorker()
    service.install_micro_live_integrity_guard(worker)

    result = worker.process_micro_live(
        FakeStore(["BBBUSDT", "AAAUSDT"], {"AAAUSDT": 5, "BBBUSDT": 5})
    )

    assert result == {
        "action": "NO_TRADE",
        "reason": "historical_backfill_retry_budget_exhausted",
        "unresolved_count": 2,
        "retryable_count": 0,
        "exhausted_count": 2,
    }
    assert worker.calls == 0


def test_micro_live_integrity_guard_delegates_when_historical_runs_resolved():
    worker = FakeWorker()
    service.install_micro_live_integrity_guard(worker)

    result = worker.process_micro_live(FakeStore([]))

    assert result == {"action": "READY"}
    assert worker.calls == 1
