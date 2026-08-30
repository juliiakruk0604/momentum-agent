from src.v24.labels import label_from_path


def test_v24_passage_respects_tp_before_sl_order():
    snapshot = {
        "symbol":"BTCUSDT",
        "snapshot_ms":1000,
        "best_ask":100.0,
    }
    path = [
        {"snapshot_ms":2000,"best_bid":100.40},
        {"snapshot_ms":3000,"best_bid":99.80},
        {"snapshot_ms":6000,"best_bid":100.10},
    ]
    out = label_from_path(snapshot, path, 5)
    assert out["passage_035_015"]["tp_before_sl"] is True
    assert out["passage_035_015"]["sl_before_tp"] is False


def test_v24_passage_respects_sl_before_tp_order():
    snapshot = {
        "symbol":"BTCUSDT",
        "snapshot_ms":1000,
        "best_ask":100.0,
    }
    path = [
        {"snapshot_ms":2000,"best_bid":99.80},
        {"snapshot_ms":3000,"best_bid":100.50},
        {"snapshot_ms":6000,"best_bid":100.20},
    ]
    out = label_from_path(snapshot, path, 5)
    assert out["passage_035_015"]["tp_before_sl"] is False
    assert out["passage_035_015"]["sl_before_tp"] is True
