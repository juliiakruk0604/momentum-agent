import time

from src.v24.book import LocalOrderBook
from src.v24.features import MicrostructureFeatureEngine


def test_v24_orderbook_snapshot_and_delta():
    book = LocalOrderBook("BTCUSDT")
    book.apply("snapshot", {
        "s": "BTCUSDT",
        "b": [["100","2"],["99","3"]],
        "a": [["101","1"],["102","4"]],
        "u": 10,
        "seq": 100,
    }, 1000)
    first = book.snapshot()
    assert first["ready"] is True
    assert first["best_bid"] == 100.0
    assert first["best_ask"] == 101.0

    book.apply("delta", {
        "s": "BTCUSDT",
        "b": [["100","0"],["100.5","1"]],
        "a": [["101","2"]],
        "u": 11,
        "seq": 101,
    }, 1100)
    second = book.snapshot()
    assert second["best_bid"] == 100.5
    assert second["best_ask_qty"] == 2.0
    assert second["seq"] == 101


def test_v24_trade_flow_requires_real_sample():
    engine = MicrostructureFeatureEngine()
    now = int(time.time() * 1000)
    engine.on_trade("BTCUSDT", {
        "time": now,
        "price": 100.0,
        "size": 0.01,
        "side": "Buy",
    })
    book = {
        "ready": True,
        "symbol": "BTCUSDT",
        "mid": 100.0,
        "spread_pct": 0.01,
        "bid_depth_5_usdt": 10000,
        "ask_depth_5_usdt": 10000,
        "book_imbalance_5": 0.0,
        "microprice_edge_bps": 0.0,
    }
    out = engine.compute("BTCUSDT", book, now)
    assert out["trade_1s"]["buy_ratio"] == 1.0
    assert out["flow_confidence_1s"] < 0.1
    assert out["microstructure_score"] < 40


def test_v24_microprice_edge_positive_when_bid_queue_heavier():
    book = LocalOrderBook("TEST")
    book.apply("snapshot", {
        "b": [["100","10"]],
        "a": [["100.1","1"]],
    }, 1000)
    snap = book.snapshot()
    assert snap["microprice"] > snap["mid"]
    assert snap["microprice_edge_bps"] > 0
