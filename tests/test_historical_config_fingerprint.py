from src.historical_backfill import HistoricalBackfillRunner


class FakeStore:
    def __init__(self):
        self.state = {
            "dataset_id": "ds",
            "start": "2026-01-01T00:00:00+00:00",
            "end": "2026-02-01T00:00:00+00:00",
            "cursor": 0,
            "universe": [{"symbol": "AAAUSDT"}],
            "complete": False,
        }

    def get_runtime(self, key):
        return {"value": self.state} if key == "historical_backfill_state" else None

    def set_runtime(self, key, value):
        if key == "historical_backfill_state":
            self.state = value


class NoProvider:
    def __getattr__(self, name):
        raise AssertionError(f"provider must not be called on config mismatch: {name}")


def test_existing_dataset_pins_config_then_blocks_drift():
    store = FakeStore()
    cfg_a = {"research": {"statuses": ["Trading"]}, "signal": {"threshold": 1}}
    cfg_b = {"research": {"statuses": ["Trading"]}, "signal": {"threshold": 2}}

    first = HistoricalBackfillRunner(NoProvider(), store, cfg_a)
    state = first.ensure_state()
    pinned = state["config_fingerprint"]
    assert pinned
    assert state["config_mismatch"] is False

    second = HistoricalBackfillRunner(NoProvider(), store, cfg_b)
    result = second.run_batch(1)

    assert result["config_mismatch"] is True
    assert result["config_fingerprint"] == pinned
    assert result["observed_config_fingerprint"] != pinned
    assert result["cursor"] == 0
    assert result["processed"] == []
    assert store.state["complete"] is False
