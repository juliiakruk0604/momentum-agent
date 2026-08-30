import pandas as pd

from src.v22.labels import label_snapshot


class FakeProvider:
    def kline_range(self, symbol, interval, start_ms, end_ms, category="spot"):
        idx = pd.to_datetime([
            "2026-08-30T09:22:00Z",
            "2026-08-30T09:23:00Z",
            "2026-08-30T09:24:00Z",
            "2026-08-30T09:25:00Z",
            "2026-08-30T09:26:00Z",
            "2026-08-30T09:27:00Z",
        ])
        return pd.DataFrame([
            {"open":100,"high":200,"low":50,"close":100,"volume":1,"turnover":1},
            {"open":100,"high":100.5,"low":99.8,"close":100.2,"volume":1,"turnover":1},
            {"open":100.2,"high":101.0,"low":100.0,"close":100.8,"volume":1,"turnover":1},
            {"open":100.8,"high":100.9,"low":99.7,"close":100.1,"volume":1,"turnover":1},
            {"open":100.1,"high":100.4,"low":99.9,"close":100.0,"volume":1,"turnover":1},
            {"open":100.0,"high":100.2,"low":99.8,"close":100.1,"volume":1,"turnover":1},
        ], index=idx)


def test_flow_label_excludes_decision_minute_ohlc():
    label = label_snapshot(
        FakeProvider(),
        {
            "symbol": "TESTUSDT",
            "snapshot_time": "2026-08-30T09:22:23Z",
            "price": 100.0,
        },
        5,
    )
    assert label["path_start"] == "2026-08-30 09:23:00+00:00"
    assert label["mfe_pct"] == 1.0
    assert label["mae_pct"] == -0.3
    assert label["hit_1"] is True
    assert label["hit_2"] is False
