from __future__ import annotations

from datetime import date, timedelta

from scripts.train_nifty_four_models import (
    REQUIRED_AVAILABILITY,
    ROLLING_SOURCE_KEYS,
    build_latest_inference_features,
    build_rolling_feature_rows,
)


def _synthetic_rows(count: int = 494):
    rows = {}
    for i in range(count):
        day = (date(2024, 1, 1) + timedelta(days=i)).isoformat()
        features = {key: float(i + 1) for key in ROLLING_SOURCE_KEYS}
        features.update({
            "spot_close": 20000.0 + i,
            "spot_high": 20100.0 + i,
            "spot_low": 19900.0 + i,
            "ce_oi": 1000.0 + i,
            "pe_oi": 1200.0 + i,
            "ce_volume": 500.0 + i,
            "pe_volume": 600.0 + i,
            "future_volume": 2000.0 + i,
            "ce_writer_build": 100.0 + i,
            "pe_writer_build": 110.0 + i,
            "ce_short_cover": 10.0 + i,
            "pe_short_cover": 11.0 + i,
            "ce_long_unwind": 5.0 + i,
            "pe_long_unwind": 6.0 + i,
            "net_option_delta_equiv_oi": 25.0 + i,
        })
        for key in REQUIRED_AVAILABILITY:
            features[key] = 1.0
        rows[day] = {
            "session_date": day,
            "feature_schema_version": 3,
            "features": features,
            "contracts": [],
        }
    return rows


def test_rolling_windows_use_exact_five_and_twenty_two_completed_sessions():
    rows = _synthetic_rows()
    samples = build_rolling_feature_rows(rows)
    assert len(samples) == 472
    assert samples[0]["origin_index"] == 21
    # pcr_oi is i+1. Origin 22 (zero-based index 21): last five are 18..22,
    # last 22 are 1..22.
    assert samples[0]["features"]["mean5__pcr_oi"] == 20.0
    assert samples[0]["features"]["mean22__pcr_oi"] == 11.5


def test_future_rows_do_not_change_an_earlier_training_input():
    rows = _synthetic_rows()
    before = build_rolling_feature_rows(rows)[0]["features"]
    later_day = sorted(rows)[22]
    rows[later_day]["features"]["pcr_oi"] = 999999.0
    after = build_rolling_feature_rows(rows)[0]["features"]
    assert before == after


def test_missing_source_value_blocks_instead_of_imputing():
    rows = _synthetic_rows()
    first_day = sorted(rows)[0]
    rows[first_day]["features"]["participant_oi_fii_future_index_net"] = None
    try:
        build_rolling_feature_rows(rows)
    except ValueError as exc:
        assert "no imputation" in str(exc)
    else:
        raise AssertionError("Missing source data must block training; it must never be imputed")


def test_latest_inference_uses_same_rolling_engine_as_training():
    rows = _synthetic_rows()
    latest = build_latest_inference_features(rows)
    assert latest["mean5__pcr_oi"] == 492.0
    assert latest["mean22__pcr_oi"] == 483.5
    assert latest["spot_return_5_sessions"] > 0
    assert latest["spot_return_22_sessions"] > 0


def test_inference_needs_enough_history_for_full_month_window():
    rows = _synthetic_rows()
    first_22 = dict(list(sorted(rows.items()))[:22])
    try:
        build_latest_inference_features(first_22)
    except ValueError as exc:
        assert "23 stored sessions" in str(exc)
    else:
        raise AssertionError("Inference must not shorten the required 22-session history")
