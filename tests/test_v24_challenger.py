from src.v24.challenger import event_gate, confirmation_gate


def strong_feature():
    return {
        "symbol":"BTCUSDT",
        "microstructure_score":88,
        "spread_pct":0.02,
        "bid_depth_5_usdt":20000,
        "ask_depth_5_usdt":18000,
        "mid":100.1,
        "price_move_1s_pct":0.05,
        "ask_depletion_1s":0.15,
        "book_imbalance_delta_1s":0.05,
        "flow_confidence_1s":0.9,
        "flow_confidence_5s":0.9,
        "trade_1s":{"buy_ratio":0.75,"total_notional":5000},
        "trade_5s":{"buy_ratio":0.68,"total_notional":15000},
    }


def test_v24_event_gate_rejects_downtrend():
    d=event_gate(strong_feature(),"TREND_DOWN")
    assert d.ready is False
    assert "market_trend_down" in d.blockers


def test_v24_confirmation_needs_persistence():
    f=strong_feature()
    armed={"symbol":"BTCUSDT","armed_at_ms":1000,"score":90,"mid":100.0}
    early=confirmation_gate(armed,f,"TREND_UP",now_ms=2000)
    assert early["confirmed"] is False
    assert "too_early" in early["blockers"]
    later=confirmation_gate(armed,f,"TREND_UP",now_ms=4000)
    assert later["confirmed"] is True
