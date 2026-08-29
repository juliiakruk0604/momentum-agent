from src.historical_backfill import HistoricalBackfillRunner


class FakeStore:
    def __init__(self):
        self.state = {
            "dataset_id": "ds",
            "start": "2026-01-01T00:00:00+00:00",
            "end": "2026-02-01T00:00:00+00:00",
            "cursor": 2,
            "universe": [
                {"symbol": "AAAUSDT"},
                {"symbol": "BBBUSDT"},
            ],
            "complete": True,
        }
        self.runs = {
            "AAAUSDT": {"symbol": "AAAUSDT", "status": "empty", "error": None},
            "BBBUSDT": {"symbol": "BBBUSDT", "status": "ok", "error": None},
        }

    def get_runtime(self, key):
        return {"value": self.state} if key == "historical_backfill_state" else None

    def set_runtime(self, key, value):
        if key == "historical_backfill_state":
            self.state = value

    def _execute(self, sqlite_sql, pg_sql, params=(), fetch=None):
        dataset_id = params[0]
        assert dataset_id == "ds"
        rows = []
        for row in self.runs.values():
            if row["status"] in {"empty", "partial", "error"} or (
                row["status"] == "ok" and str(row.get("error") or "").startswith("exact5_errors=")
            ):
                rows.append(dict(row))
        return sorted(rows, key=lambda row: row["symbol"])

    def record_historical_symbol_run(self, dataset_id, symbol, status, bars15, impulses, oi_rows, funding_rows, error):
        self.runs[symbol] = {"symbol": symbol, "status": status, "error": error}


class RetryRunner(HistoricalBackfillRunner):
    def _process_symbol(self, state, meta):
        symbol = meta["symbol"]
        self.store.record_historical_symbol_run(state["dataset_id"], symbol, "ok", 100, 1, 100, 10, None)
        return {"symbol": symbol, "status": "ok", "impulses": 1}


def test_completed_primary_backfill_retries_empty_symbol_before_final_completion(monkeypatch):
    monkeypatch.setenv("HISTORICAL_BACKFILL_RETRY_ATTEMPTS", "2")
    store = FakeStore()
    runner = RetryRunner(provider=None, store=store, cfg={})

    result = runner.run_batch(1)

    assert result["complete"] is True
    assert result["processed"] == [
        {"symbol": "AAAUSDT", "status": "ok", "impulses": 1, "retry": True, "attempt": 1}
    ]
    assert result["unresolved_retry_symbols"] == []
    assert store.state["retry_attempts"]["AAAUSDT"] == 1
    assert store.runs["AAAUSDT"]["status"] == "ok"
