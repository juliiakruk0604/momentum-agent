from v24_stream_service import _latest_v22_map


class FakeStore:
    def get_runtime(self, key):
        assert key == "v22_fast_scan"
        return {
            "value": {
                "fast_universe": [
                    {"symbol":"BTCUSDT","score":10,"score_kind":"coarse_fast_only"},
                    {"symbol":"ETHUSDT","score":20,"score_kind":"coarse_fast_only"},
                ],
                "candidates": [
                    {"symbol":"BTCUSDT","score":88,"flow_score":70,"risk":{"allowed":True}},
                ],
            }
        }


def test_v24_base_map_keeps_full_universe_and_enriched_override():
    out = _latest_v22_map(FakeStore())
    assert set(out) == {"BTCUSDT","ETHUSDT"}
    assert out["BTCUSDT"]["score"] == 88
    assert out["BTCUSDT"]["flow_score"] == 70
    assert out["ETHUSDT"]["score_kind"] == "coarse_fast_only"
