import api


class FakeStore:
    def __init__(self):
        self.state = {
            "universe": [
                {"symbol": "CLOSEDUSDT", "status": "Closed", "deliveryTime": 0},
                {"symbol": "LIVEUSDT", "status": "Trading", "deliveryTime": 0},
            ]
        }

    def _execute(self, sqlite_sql, pg_sql, params=(), fetch=None):
        sql = sqlite_sql
        if "status='empty'" in sql and "SELECT symbol" in sql:
            return [{"symbol": "CLOSEDUSDT"}, {"symbol": "LIVEUSDT"}]
        if "exact5_errors" in sql:
            return {"n": 0}
        raise AssertionError(sql)

    def get_runtime(self, key):
        assert key == "historical_backfill_state"
        return {"value": self.state}


def test_unavailable_closed_contract_is_separate_and_fail_closed(monkeypatch):
    monkeypatch.setattr(api, "store", FakeStore())
    historical = {
        "dataset_id": "ds",
        "status": "complete",
        "progress": {
            "quality": [
                {"status": "empty", "n": 2},
                {"status": "ok", "n": 731},
            ]
        },
        "oos": {"research_gate": {"passed": True, "reasons": []}},
    }

    result = api._historical_status_with_integrity_gate(historical)

    assert result["integrity"]["backfill_empty_run_count"] == 2
    assert result["integrity"]["backfill_unavailable_market_data_count"] == 1
    assert result["integrity"]["backfill_unavailable_market_data_symbols"] == ["CLOSEDUSDT"]
    assert result["integrity"]["backfill_transient_empty_count"] == 1
    assert result["oos"]["research_gate"]["passed"] is False
    assert "historical_backfill_has_unavailable_market_data" in result["oos"]["research_gate"]["reasons"]
    assert "historical_backfill_has_empty_runs" in result["oos"]["research_gate"]["reasons"]
