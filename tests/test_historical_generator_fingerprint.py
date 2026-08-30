from src import historical_backfill
from src.historical_backfill import HistoricalBackfillRunner


class FakeStore:
    def __init__(self, state):
        self.state = state

    def get_runtime(self, key):
        return {"value": self.state} if key == "historical_backfill_state" else None

    def set_runtime(self, key, value):
        if key == "historical_backfill_state":
            self.state = value


class NoProvider:
    def __getattr__(self, name):
        raise AssertionError(f"provider must not be called when provenance is unverified: {name}")


def test_legacy_dataset_without_generator_fingerprint_fails_closed(monkeypatch):
    monkeypatch.setattr(historical_backfill, "_generator_fingerprint", lambda: "code-v2")
    cfg = {"research": {"statuses": ["Trading"]}, "signal": {"threshold": 1}}
    store = FakeStore({
        "dataset_id": "legacy-ds",
        "start": "2026-01-01T00:00:00+00:00",
        "end": "2026-02-01T00:00:00+00:00",
        "cursor": 1,
        "universe": [{"symbol": "AAAUSDT"}],
        "config_fingerprint": historical_backfill._config_fingerprint(cfg),
        "complete": True,
    })

    result = HistoricalBackfillRunner(NoProvider(), store, cfg).run_batch(1)

    assert result["complete"] is False
    assert result["generator_provenance_missing"] is True
    assert result["observed_generator_fingerprint"] == "code-v2"
    assert result["processed"] == []
    assert store.state["complete"] is False


def test_pinned_generator_drift_fails_closed(monkeypatch):
    monkeypatch.setattr(historical_backfill, "_generator_fingerprint", lambda: "code-v2")
    cfg = {"research": {"statuses": ["Trading"]}, "signal": {"threshold": 1}}
    store = FakeStore({
        "dataset_id": "pinned-ds",
        "start": "2026-01-01T00:00:00+00:00",
        "end": "2026-02-01T00:00:00+00:00",
        "cursor": 1,
        "universe": [{"symbol": "AAAUSDT"}],
        "config_fingerprint": historical_backfill._config_fingerprint(cfg),
        "generator_fingerprint": "code-v1",
        "complete": True,
    })

    result = HistoricalBackfillRunner(NoProvider(), store, cfg).run_batch(1)

    assert result["complete"] is False
    assert result["generator_provenance_missing"] is False
    assert result["generator_mismatch"] is True
    assert result["generator_fingerprint"] == "code-v1"
    assert result["observed_generator_fingerprint"] == "code-v2"
    assert result["processed"] == []
