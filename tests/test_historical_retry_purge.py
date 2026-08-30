from src.historical_backfill import HistoricalBackfillRunner, _generator_fingerprint


class FakeStore:
    def __init__(self):
        self.state = {
            "dataset_id": "ds",
            "start": "2026-01-01T00:00:00+00:00",
            "end": "2026-02-01T00:00:00+00:00",
            "cursor": 1,
            "universe": [{"symbol": "AAAUSDT"}],
            "generator_fingerprint": _generator_fingerprint(),
            "complete": True,
        }
        self.runs = {
            "AAAUSDT": {"symbol": "AAAUSDT", "status": "partial", "error": "exact5_errors=1"}
        }
        self.events = {"AAAUSDT": {"old-signal", "stale-signal"}}

    def get_runtime(self, key):
        return {"value": self.state} if key == "historical_backfill_state" else None

    def set_runtime(self, key, value):
        if key == "historical_backfill_state":
            self.state = value

    def _execute(self, sqlite_sql, pg_sql, params=(), fetch=None):
        if sqlite_sql.startswith("DELETE FROM historical_events"):
            dataset_id, symbol = params
            assert dataset_id == "ds"
            self.events[symbol] = set()
            return None
        dataset_id = params[0]
        assert dataset_id == "ds"
        rows = []
        for row in self.runs.values():
            if row["status"] in {"empty", "partial", "error"} or (
                row["status"] == "ok" and str(row.get("error") or "").startswith("exact5_errors=")
            ):
                rows.append(dict(row))
        return rows

    def record_historical_symbol_run(self, dataset_id, symbol, status, bars15, impulses, oi_rows, funding_rows, error):
        self.runs[symbol] = {"symbol": symbol, "status": status, "error": error}


class RetryRunner(HistoricalBackfillRunner):
    def _process_symbol(self, state, meta):
        symbol = meta["symbol"]
        assert self.store.events[symbol] == set()
        self.store.events[symbol].add("fresh-signal")
        self.store.record_historical_symbol_run(state["dataset_id"], symbol, "ok", 100, 1, 100, 10, None)
        return {"symbol": symbol, "status": "ok", "impulses": 1}


def test_retry_purges_stale_symbol_events_before_rebuild(monkeypatch):
    monkeypatch.setenv("HISTORICAL_BACKFILL_RETRY_ATTEMPTS", "2")
    store = FakeStore()
    runner = RetryRunner(provider=None, store=store, cfg={})

    result = runner.run_batch(1)

    assert result["complete"] is True
    assert store.events["AAAUSDT"] == {"fresh-signal"}
    assert store.runs["AAAUSDT"]["status"] == "ok"
