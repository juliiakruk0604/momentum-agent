from src.v24.sequence import SequenceFeatureEngine


def feature(score, buy=0.6, lead=0.01):
    return {
        "symbol":"BTCUSDT",
        "microstructure_score":score,
        "book_imbalance_5":0.2,
        "ask_depletion_1s":0.1,
        "price_move_1s_pct":0.02,
        "flow_confidence_1s":0.8,
        "trade_1s":{"buy_ratio":buy},
        "cross_exchange":{
            "external_minus_bybit_move_1s_pct":lead,
            "external_consensus_move_1s_pct":0.03,
        },
        "perp_context":{"oi_change_5s_pct":0.02},
    }


def test_sequence_uses_only_prior_points():
    engine = SequenceFeatureEngine()
    a = engine.enrich(feature(60), now_ms=1000)
    b = engine.enrich(feature(70), now_ms=2000)
    c = engine.enrich(feature(80), now_ms=4000)

    assert a["sequence_context"]["score_delta_1s"] == 0.0
    assert b["sequence_context"]["score_delta_1s"] == 10.0
    assert c["sequence_context"]["score_delta_3s"] == 20.0
    assert c["sequence_context"]["score_mean_3s"] >= 70.0
