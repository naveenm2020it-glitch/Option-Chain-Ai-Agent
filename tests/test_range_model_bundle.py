from __future__ import annotations

import numpy as np
import pandas as pd

from scripts.range_model_bundle import fit_range_bundle, predict_range_bundle
from scripts.range_shadow_validation import (
    advance_shadow_state,
    make_range_forecast,
    score_range_forecast,
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


def test_range_bundle_produces_finite_ordered_next_session_returns():
    bundle, features = _bundle_and_features()
    forecast = predict_range_bundle(bundle, features)
    assert np.isfinite(list(forecast.values())).all()
    assert forecast["low_return"] < forecast["high_return"]
    assert forecast["low_return"] <= forecast["close_return"] <= forecast["high_return"]


def test_range_forecast_translates_returns_to_positive_price_levels():
    bundle, features = _bundle_and_features()
    forecast = make_range_forecast(bundle, "2026-11-16", 25000.0, features)
    assert forecast["status"] == "pending"
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
