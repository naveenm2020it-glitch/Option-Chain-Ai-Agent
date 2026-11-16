from __future__ import annotations

from datetime import date, timedelta
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd
import pytest

import scripts.range_shadow_validation as shadow

from scripts.range_model_bundle import fit_range_bundle, predict_range_bundle
from scripts.range_research_benchmark import _asymmetric_conformal_adjustment, _conformal_adjustment
from scripts.range_shadow_validation import (
    advance_shadow_state,
    make_range_forecast,
    make_recent_22_session_baseline,
    score_range_forecast,
    summarize_shadow_records,
)


def _bundle_and_features():
    rng = np.random.default_rng(7)
    count = 180
    vol = rng.uniform(0.003, 0.018, count)
    momentum = rng.normal(0.0, 0.004, count)
    X = pd.DataFrame({
        "realized_volatility_5_sessions": vol,
        "realized_volatility_22_sessions": np.maximum(vol * 0.85, 0.001),
        "latest__spot_intraday_range_pct": vol * 1.4,
        "mean5__spot_intraday_range_pct": vol * 1.2,
        "momentum_signal": momentum,
    })
    width = vol * 1.6 + 0.002
    close = momentum * 0.25 + rng.normal(0.0, 0.001, count)
    y = np.column_stack((
        -width + rng.normal(0.0, 0.001, count),
        width + rng.normal(0.0, 0.001, count),
        close,
    ))
    candidate = {
        "name": "test_extra_trees",
        "family": "extra_trees_quantile",
        "config": {
            "trees": 24,
            "max_depth": 4,
            "min_samples_leaf": 6,
            "max_features": 0.8,
        },
    }
    bundle = fit_range_bundle(candidate, X, y)
    features = X.iloc[-1].to_dict()
    return bundle, features




def test_conformal_calibration_never_shrinks_interval_and_expands_for_misses():
    X = pd.DataFrame({
        "realized_volatility_5_sessions": [0.01] * 30,
        "realized_volatility_22_sessions": [0.01] * 30,
    })
    predictions = (
        np.full(30, -0.02),
        np.full(30, 0.02),
        np.zeros(30),
    )
    fully_covered = np.column_stack((
        np.full(30, -0.01),
        np.full(30, 0.01),
        np.zeros(30),
    ))
    # All rows are already inside the base interval: calibration must not
    # return a negative margin that contracts it.
    assert _conformal_adjustment(predictions, fully_covered, X) == 0.0

    misses = fully_covered.copy()
    misses[-1, 1] = 0.04
    adjustment = _conformal_adjustment(predictions, misses, X)
    assert adjustment >= 0.0






def test_conformal_calibration_rejects_misaligned_or_impossible_targets():
    X = pd.DataFrame({
        "realized_volatility_5_sessions": [0.01] * 3,
        "realized_volatility_22_sessions": [0.01] * 3,
    })
    predictions = (
        np.full(3, -0.02),
        np.full(3, 0.02),
        np.zeros(3),
    )
    valid = np.column_stack((np.full(3, -0.01), np.full(3, 0.01), np.zeros(3)))
    with pytest.raises(ValueError, match="aligned"):
        _conformal_adjustment(predictions, valid[:2], X)
    impossible = valid.copy()
    impossible[0, 2] = 0.02
    with pytest.raises(ValueError, match="OHLC return ordering"):
        _conformal_adjustment(predictions, impossible, X)
    with pytest.raises(ValueError, match="alpha"):
        _conformal_adjustment(predictions, valid, X, alpha=1.0)


def test_range_bundle_rejects_impossible_ohlc_targets():
    rng = np.random.default_rng(11)
    count = 180
    X = pd.DataFrame({
        "realized_volatility_5_sessions": rng.uniform(0.003, 0.018, count),
        "realized_volatility_22_sessions": rng.uniform(0.003, 0.018, count),
    })
    y = np.column_stack((
        np.full(count, -0.01),
        np.full(count, 0.01),
        np.zeros(count),
    ))
    y[12, 2] = 0.02  # close above the session high is impossible
    candidate = {
        "name": "test_extra_trees",
        "family": "extra_trees_quantile",
        "config": {"trees": 12, "max_depth": 3, "min_samples_leaf": 5, "max_features": 0.8},
    }
    with pytest.raises(ValueError, match="Range targets must satisfy"):
        fit_range_bundle(candidate, X, y)


def test_range_inference_rejects_empty_or_multirow_inputs_and_negative_margin():
    bundle, features = _bundle_and_features()
    with pytest.raises(ValueError, match="exactly one"):
        predict_range_bundle(bundle, pd.DataFrame())
    with pytest.raises(ValueError, match="exactly one"):
        predict_range_bundle(bundle, pd.DataFrame([features, features]))
    corrupted = dict(bundle, normalized_adjustment={"low": -0.01, "high": 0.0})
    with pytest.raises(ValueError, match="nonnegative"):
        predict_range_bundle(corrupted, features)


def test_asymmetric_conformal_calibration_keeps_endpoint_margins_separate():
    X = pd.DataFrame({
        "realized_volatility_5_sessions": [0.01] * 40,
        "realized_volatility_22_sessions": [0.01] * 40,
    })
    predictions = (
        np.full(40, -0.02),
        np.full(40, 0.02),
        np.zeros(40),
    )
    actual = np.column_stack((
        np.full(40, -0.01),
        np.full(40, 0.03),
        np.zeros(40),
    ))
    margins = _asymmetric_conformal_adjustment(predictions, actual, X, alpha=0.10)
    assert set(margins) == {"low", "high"}
    assert margins["low"] == pytest.approx(0.0)
    assert margins["high"] > margins["low"]


def test_range_bundle_produces_finite_ordered_next_session_returns():
    bundle, features = _bundle_and_features()
    forecast = predict_range_bundle(bundle, features)
    assert np.isfinite(list(forecast.values())).all()
    assert forecast["low_return"] < forecast["high_return"]
    assert forecast["low_return"] <= forecast["close_return"] <= forecast["high_return"]
    assert bundle["calibration_alpha"] == 0.10


def test_range_forecast_translates_returns_to_positive_price_levels():
    bundle, features = _bundle_and_features()
    forecast = make_range_forecast(bundle, "2026-11-16", 25000.0, features)
    assert forecast["status"] == "pending"
    assert 0 < forecast["core_predicted_low"] <= forecast["core_predicted_high"]
    assert forecast["core_low_return"] <= forecast["core_close_return"] <= forecast["core_high_return"]
    assert 0 < forecast["predicted_low"] <= forecast["predicted_close"]
    assert forecast["predicted_close"] <= forecast["predicted_high"]


def test_shadow_scoring_uses_actual_range_and_skips_stale_forecasts():
    forecast = {
        "origin_date": "2026-11-16",
        "origin_close": 25000.0,
        "predicted_low_return": -0.01,
        "predicted_high_return": 0.01,
        "predicted_low": 24750.0,
        "predicted_high": 25250.0,
        "predicted_close": 25010.0,
        "candidate_name": "test",
        "calibration_alpha": 0.20,
    }
    actual = {"spot_low": 24800.0, "spot_high": 25200.0, "spot_close": 25100.0}
    scored = score_range_forecast(forecast, "2026-11-17", actual)
    assert scored["status"] == "scored"
    assert scored["range_covered"] is True
    assert scored["close_inside_predicted_range"] is True
    stale = score_range_forecast(forecast, "2026-11-23", actual)
    assert stale["status"] == "skipped_stale_gap"

    # A missing Tuesday/Wednesday report must not cause a later weekday to be
    # mislabeled as the next-session outcome.
    nonadjacent = score_range_forecast(forecast, "2026-11-18", actual)
    assert nonadjacent["status"] == "skipped_nonadjacent_session"
    assert nonadjacent["weekday_steps"] == 2

    # A normal Friday-to-Monday weekend remains a valid next-session gap.
    friday_forecast = dict(forecast, origin_date="2026-11-20")
    monday = score_range_forecast(friday_forecast, "2026-11-23", actual)
    assert monday["status"] == "scored"


def test_shadow_state_is_idempotent_and_scores_only_subsequent_session():
    bundle, features = _bundle_and_features()
    state = {"schema_version": 1, "records": [], "pending_forecast": None}
    first_row = {
        "session_date": "2026-11-16",
        "features": {"spot_close": 25000.0},
    }
    state, first = advance_shadow_state(state, first_row, features, bundle)
    assert first["status"] == "forecast_created"
    assert state["pending_forecast"]["origin_date"] == "2026-11-16"

    same_state, duplicate = advance_shadow_state(state, first_row, features, bundle)
    assert duplicate["status"] == "same_session_idempotent"
    assert len(same_state["records"]) == 0

    next_row = {
        "session_date": "2026-11-17",
        "features": {"spot_low": 24800.0, "spot_high": 25200.0, "spot_close": 25100.0},
    }
    state, next_report = advance_shadow_state(state, next_row, features, bundle)
    assert next_report["newly_evaluated_record"]["status"] == "scored"
    assert state["records"][0]["actual_session_date"] == "2026-11-17"
    assert state["pending_forecast"]["origin_date"] == "2026-11-17"


def test_recent_22_session_baseline_is_leakage_safe_and_uses_22_completed_ranges():
    rows = {}
    start = date(2026, 9, 1)
    last_date = None
    for index in range(30):
        day = (start + timedelta(days=index)).isoformat()
        close = 25000.0 + index * 10.0
        rows[day] = {
            "session_date": day,
            "features": {
                "spot_close": close,
                "spot_low": close - 100.0 - (index % 4) * 5.0,
                "spot_high": close + 90.0 + (index % 3) * 5.0,
            },
        }
        last_date = day

    origin_close = rows[last_date]["features"]["spot_close"]
    baseline = make_recent_22_session_baseline(rows, last_date, origin_close)
    future_date = (date.fromisoformat(last_date) + timedelta(days=1)).isoformat()
    rows[future_date] = {
        "session_date": future_date,
        "features": {"spot_close": 35000.0, "spot_low": 30000.0, "spot_high": 40000.0},
    }
    baseline_with_future_row = make_recent_22_session_baseline(rows, last_date, origin_close)

    assert baseline["lookback_sessions"] == 22
    assert baseline["predicted_low"] < baseline["predicted_close"] < baseline["predicted_high"]
    assert baseline == baseline_with_future_row


def test_shadow_scoring_compares_model_with_rolling_baseline_and_sets_review_gate():
    forecast = {
        "origin_date": "2026-11-16",
        "origin_close": 25000.0,
        "predicted_low_return": -0.01,
        "predicted_high_return": 0.01,
        "predicted_close_return": 0.0004,
        "predicted_low": 24750.0,
        "predicted_high": 25250.0,
        "predicted_close": 25010.0,
        "candidate_name": "test",
        "calibration_alpha": 0.10,
        "baseline": {
            "name": "rolling_22_session_empirical_quantile",
            "predicted_low_return": -0.005,
            "predicted_high_return": 0.005,
            "predicted_close_return": 0.0,
            "predicted_low": 24875.0,
            "predicted_high": 25125.0,
            "predicted_close": 25000.0,
        },
    }
    actual = {"spot_low": 24800.0, "spot_high": 25200.0, "spot_close": 25100.0}
    scored = score_range_forecast(forecast, "2026-11-17", actual)
    assert scored["status"] == "scored"
    assert scored["range_covered"] is True
    assert scored["baseline_range_covered"] is False
    assert scored["model_beats_baseline_winkler"] is True
    assert scored["predicted_range_width_points"] == pytest.approx(500.0)
    assert scored["actual_range_width_points"] == pytest.approx(400.0)
    assert scored["range_width_difference_points"] == pytest.approx(100.0)
    assert scored["range_width_ratio"] == pytest.approx(1.25)
    assert scored["low_absolute_error_points"] == pytest.approx(50.0)
    assert scored["high_absolute_error_points"] == pytest.approx(50.0)
    assert scored["baseline_predicted_range_width_points"] == pytest.approx(250.0)

    summary = summarize_shadow_records([scored] * 20)
    assert summary["scored_sessions"] == 20
    assert summary["mean_model_predicted_width_points"] == pytest.approx(500.0)
    assert summary["mean_actual_width_points"] == pytest.approx(400.0)
    assert summary["mean_model_width_difference_points"] == pytest.approx(100.0)
    assert summary["mean_model_width_ratio"] == pytest.approx(1.25)
    assert summary["mean_model_high_mae_points"] == pytest.approx(50.0)
    assert summary["mean_model_low_mae_points"] == pytest.approx(50.0)
    assert summary["mean_baseline_predicted_width_points"] == pytest.approx(250.0)
    assert summary["shadow_validation_status"] == "pass_manual_review"
    assert summary["production_promotion_allowed"] is False
    assert summary["skipped_nonadjacent_session_count"] == 0


def test_current_nse_feature_extraction_blocks_missing_critical_context(tmp_path, monkeypatch):
    report_names = (
        "fo_udiff_bhavcopy",
        "fo_participant_oi",
        "fo_participant_volume",
        "nifty_index_close_all",
    )
    results = {}
    for name in report_names:
        filename = f"{name}.csv"
        (tmp_path / filename).write_text("fixture", encoding="utf-8")
        results[name] = {"file": filename, "report_date": "2026-11-16"}
    manifest = {
        "core_session_status": "aligned",
        "core_session_date": "2026-11-16",
        "results": results,
    }
    (tmp_path / "manifest.json").write_text(__import__("json").dumps(manifest), encoding="utf-8")

    valid_features = {
        "index_data_available": 1.0,
        "participant_oi_data_available": 1.0,
        "participant_volume_data_available": 1.0,
        "future_data_available": 1.0,
        "spot_open": 25000.0,
        "spot_high": 25100.0,
        "spot_low": 24900.0,
        "spot_close": 25050.0,
    }
    monkeypatch.setattr(
        shadow,
        "extract_nifty_features",
        lambda *_: {
            "session_date": "2026-11-16",
            "feature_schema_version": shadow.FEATURE_SCHEMA_VERSION,
            "features": valid_features,
            "contracts": [],
        },
    )
    row = shadow.extract_current_feature_row(tmp_path)
    assert row["session_date"] == "2026-11-16"

    missing_context = dict(valid_features)
    missing_context["participant_volume_data_available"] = 0.0
    monkeypatch.setattr(
        shadow,
        "extract_nifty_features",
        lambda *_: {
            "session_date": "2026-11-16",
            "feature_schema_version": shadow.FEATURE_SCHEMA_VERSION,
            "features": missing_context,
            "contracts": [],
        },
    )
    with pytest.raises(ValueError, match="participant_volume_data_available"):
        shadow.extract_current_feature_row(tmp_path)


def test_run_shadow_creates_forecast_with_verified_manifest_and_zero_b2(tmp_path, monkeypatch):
    rows = {}
    start = date(2024, 1, 1)
    for index in range(494):
        day = (start + timedelta(days=index)).isoformat()
        close = 20000.0 + index * 2.0
        rows[day] = {
            "session_date": day,
            "feature_schema_version": shadow.FEATURE_SCHEMA_VERSION,
            "features": {
                "spot_close": close,
                "spot_low": close - 80.0,
                "spot_high": close + 90.0,
            },
            "contracts": [],
        }
    origin_date = max(rows)
    next_date = (date.fromisoformat(origin_date) + timedelta(days=1)).isoformat()
    current_row = {
        "session_date": next_date,
        "feature_schema_version": shadow.FEATURE_SCHEMA_VERSION,
        "features": {
            "spot_close": 21000.0,
            "spot_low": 20900.0,
            "spot_high": 21100.0,
        },
        "contracts": [],
    }

    checkpoint_path = tmp_path / "checkpoint.json"
    checkpoint_path.write_text(__import__("json").dumps({"features": rows}), encoding="utf-8")
    reports_dir = tmp_path / "reports"
    reports_dir.mkdir()
    bundle_dir = tmp_path / "models"
    bundle_dir.mkdir()
    bundle_path = bundle_dir / "nifty_range_candidate.joblib"
    bundle_path.write_bytes(b"verified test candidate")
    candidate = {
        "schema_version": 1,
        "candidate": {"name": "test_range"},
        "feature_names": ["feature_a"],
        "calibration_alpha": 0.10,
    }
    checksum = __import__("hashlib").sha256(bundle_path.read_bytes()).hexdigest()
    manifest = {
        "status": "range_research_candidate_built",
        "forward_shadow_validation_required": True,
        "deployment_ready": False,
        "production_promotion_allowed": False,
        "archive_sessions": 494,
        "archive_audit_pass": True,
        "context_repair_pass": True,
        "feature_count": 1,
        "calibration_alpha": 0.10,
        "model_sha256": checksum,
    }
    (bundle_dir / "range_candidate_manifest.json").write_text(
        __import__("json").dumps(manifest), encoding="utf-8"
    )

    monkeypatch.setattr(shadow, "extract_current_feature_row", lambda _: current_row)
    monkeypatch.setattr(shadow, "build_latest_inference_features", lambda _: {"feature_a": 1.0})
    monkeypatch.setattr(shadow.joblib, "load", lambda _: candidate)
    monkeypatch.setattr(
        shadow,
        "predict_range_bundle",
        lambda *_: {
            "low_return": -0.01,
            "high_return": 0.01,
            "close_return": 0.0,
            "conformal_margin_return": 0.001,
        },
    )

    state_path = tmp_path / "range_shadow_state.json"
    output_path = tmp_path / "range_shadow_report.json"
    report = shadow.run_shadow(
        checkpoint_path, reports_dir, bundle_path, state_path, output_path
    )
    state = __import__("json").loads(state_path.read_text(encoding="utf-8"))

    assert report["status"] == "forecast_created"
    assert report["b2_reads"] == 0 and report["b2_writes"] == 0
    assert report["shadow_validation_status"] == "collecting"
    assert state["pending_forecast"]["baseline"]["name"] == "rolling_22_session_empirical_quantile"
    assert state["pending_forecast"]["calibration_alpha"] == 0.10
