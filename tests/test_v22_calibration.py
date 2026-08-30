from src.v22.calibration import _score_rule


def test_calibration_rejects_tiny_lucky_subset(monkeypatch):
    monkeypatch.setenv("V22_CALIBRATION_MIN_TRAIN_SUBSET", "25")
    monkeypatch.setenv("V22_CALIBRATION_MIN_VALID_SUBSET", "15")

    base_train = {
        "p_hit_0_5": 0.05,
        "avg_mfe_pct": 0.10,
    }
    base_valid = {
        "p_hit_0_5": 0.05,
        "avg_mfe_pct": 0.10,
    }
    train = {
        "n": 11,
        "p_hit_0_5": 0.30,
        "p_hit_0_5_wilson_lower_90": 0.15,
        "avg_mfe_pct": 0.50,
        "avg_final_return_pct": 0.30,
    }
    valid = {
        "n": 12,
        "p_hit_0_5": 0.60,
        "p_hit_0_5_wilson_lower_90": 0.35,
        "avg_mfe_pct": 1.20,
        "avg_final_return_pct": 0.80,
    }
    assert _score_rule(train, valid, base_train, base_valid) is None
