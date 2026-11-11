from nifty_ai.eod_intelligence import _wilson, walk_forward_zone_validation, build_defence_map


def test_wilson_interval_is_bounded_and_contains_rate():
    low, high = _wilson(8, 10)
    assert 0 <= low <= 0.8 <= high <= 1


def test_walk_forward_validation_does_not_claim_calibration_without_prediction_log():
    rows = []
    for i in range(45):
        base = 22000 + (i % 5) * 5
        rows.append({"date": f"2025-01-{i+1:02d}", "open": base, "high": base + 30,
                     "low": base - 30, "close": base + (5 if i % 2 else -5)})
    result = walk_forward_zone_validation(rows)
    assert result["forecast_probability_calibration"] == "NOT_CALIBRATED_NO_PROSPECTIVE_PROBABILITY_LOG"
    assert result["sessions_available"] == 45


def test_defence_map_keeps_buyer_and_seller_sides_separate():
    rows = [{"open": 100, "high": 110, "low": 90, "close": 105}]
    options = {"available": True, "top_put_oi_strike": 90, "top_call_oi_strike": 110,
               "top_put_oi_share_pct": 40, "top_call_oi_share_pct": 45,
               "put_writing_strikes": [90], "call_writing_strikes": [110]}
    levels = {"support": [], "resistance": []}
    result = build_defence_map(rows, options, levels)
    assert result["buyer_defence"][0]["center"] == 90
    assert result["seller_defence"][0]["center"] == 110
    assert result["score_type"] == "EVIDENCE_SCORE_NOT_PROBABILITY"
