import service


class FakeStore:
    def __init__(self, unresolved):
        self.unresolved = unresolved

    def get_runtime(self, key):
        assert key == "historical_backfill_state"
        return {"value": {"unresolved_retry_symbols": self.unresolved}}


class FakeWorker:
    def __init__(self):
        self.calls = 0

    def process_micro_live(self, store):
        self.calls += 1
        return {"action": "READY"}


def test_micro_live_integrity_guard_blocks_unresolved_historical_runs():
    worker = FakeWorker()
    service.install_micro_live_integrity_guard(worker)

    result = worker.process_micro_live(FakeStore(["BBBUSDT", "AAAUSDT", "AAAUSDT"]))

    assert result == {
        "action": "NO_TRADE",
        "reason": "historical_backfill_has_unresolved_runs",
        "unresolved_count": 2,
    }
    assert worker.calls == 0


def test_micro_live_integrity_guard_delegates_when_historical_runs_resolved():
    worker = FakeWorker()
    service.install_micro_live_integrity_guard(worker)

    result = worker.process_micro_live(FakeStore([]))

    assert result == {"action": "READY"}
    assert worker.calls == 1
