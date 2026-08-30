import worker


class FakeStore:
    def __init__(self):
        self.runtime = {}

    def get_runtime(self, key):
        if key not in self.runtime:
            return None
        return {"value": self.runtime[key]}

    def set_runtime(self, key, value):
        self.runtime[key] = value


class FakeBackfill:
    def __init__(self, store):
        self.store = store
        self.new_state_calls = 0
        self.state = {
            "dataset_id": "legacy-ds",
            "cursor": 2,
            "universe": [{"symbol": "AAAUSDT"}, {"symbol": "BBBUSDT"}],
            "generator_provenance_missing": True,
            "complete": False,
        }

    def ensure_state(self):
        return self.state

    def _new_state(self):
        self.new_state_calls += 1
        self.state = {
            "dataset_id": "verified-ds",
            "cursor": 0,
            "universe": [{"symbol": "AAAUSDT"}, {"symbol": "BBBUSDT"}],
            "generator_provenance_missing": False,
            "complete": False,
        }
        self.store.set_runtime("historical_backfill_state", self.state)
        return self.state


def test_completed_legacy_dataset_is_archived_before_rebuild():
    store = FakeStore()
    backfill = FakeBackfill(store)

    result = worker._prepare_provenance_rebuild(backfill, store)

    assert result == {
        "reason": "generator_provenance_missing",
        "from_dataset_id": "legacy-ds",
        "to_dataset_id": "verified-ds",
    }
    archived = store.runtime["historical_backfill_superseded:legacy-ds"]
    assert archived["dataset_id"] == "legacy-ds"
    assert archived["superseded_reason"] == "generator_provenance_missing"
    assert backfill.new_state_calls == 1
    assert store.runtime["historical_backfill_state"]["dataset_id"] == "verified-ds"


def test_incomplete_legacy_dataset_stays_fail_closed():
    store = FakeStore()
    backfill = FakeBackfill(store)
    backfill.state["cursor"] = 1

    result = worker._prepare_provenance_rebuild(backfill, store)

    assert result is None
    assert backfill.new_state_calls == 0
    assert "historical_backfill_superseded:legacy-ds" not in store.runtime
