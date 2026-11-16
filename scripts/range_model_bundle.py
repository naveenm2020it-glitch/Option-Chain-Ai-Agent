from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

from scripts.range_research_benchmark import (
    ALPHA,
    Q_HIGH,
    Q_LOW,
    _base_bounds,
    _conformal_adjustment,
    _make_sklearn_estimator,
    _tree_distribution_prediction,
    _volatility_neighbours,
    _volatility_scale,
)


def _fit_estimators(candidate: dict[str, Any], X_fit: pd.DataFrame, y_fit: np.ndarray):
    family = candidate["family"]
    config = candidate.get("config", {})
    if family == "volatility_neighbours":
        return []

    fit_scale = (
        _volatility_scale(X_fit)
        if candidate.get("volatility_scaled", False)
        else np.ones(len(X_fit), dtype=float)
    )
    scaled_y = y_fit / fit_scale[:, None]
    models = []

    if family == "lightgbm_quantile":
        from lightgbm import LGBMRegressor

        for target_column, quantile in ((0, Q_LOW), (1, Q_HIGH), (2, None)):
            params = {
                "n_estimators": config["n_estimators"],
                "learning_rate": config["learning_rate"],
                "num_leaves": config["num_leaves"],
                "max_depth": config["max_depth"],
                "min_child_samples": config["min_child_samples"],
                "reg_lambda": config["reg_lambda"],
                "reg_alpha": config["reg_alpha"],
                "colsample_bytree": 0.8,
                "subsample": 0.85,
                "subsample_freq": 1,
                "verbosity": -1,
                "random_state": 42,
                "n_jobs": 2,
            }
            if quantile is None:
                model = LGBMRegressor(objective="regression_l1", **params)
            else:
                model = LGBMRegressor(objective="quantile", alpha=quantile, **params)
            model.fit(X_fit, scaled_y[:, target_column])
            models.append(model)
        return models

    for target_column, quantile in ((0, Q_LOW), (1, Q_HIGH), (2, None)):
        model = _make_sklearn_estimator(family, quantile, config)
        model.fit(X_fit, scaled_y[:, target_column])
        models.append(model)
    return models


def _predict_base(bundle: dict[str, Any], X_eval: pd.DataFrame):
    candidate = bundle["candidate"]
    family = candidate["family"]
    eval_scale = (
        _volatility_scale(X_eval)
        if candidate.get("volatility_scaled", False)
        else np.ones(len(X_eval), dtype=float)
    )

    if family == "volatility_neighbours":
        raw = _volatility_neighbours(
            bundle["fit_X"],
            bundle["fit_y_scaled"],
            X_eval,
            int(candidate.get("config", {}).get("k", 40)),
        )
        return tuple(np.asarray(item, dtype=float) * eval_scale for item in raw)

    predictions = []
    for model, quantile in zip(bundle["models"], (Q_LOW, Q_HIGH, None)):
        if family in ("extra_trees_quantile", "random_forest_quantile"):
            prediction = _tree_distribution_prediction(
                model, X_eval, 0.50 if quantile is None else quantile
            )
        else:
            prediction = model.predict(X_eval)
        predictions.append(np.asarray(prediction, dtype=float) * eval_scale)
    return predictions[0], predictions[1], predictions[2]


def fit_range_bundle(
    candidate: dict[str, Any],
    X: pd.DataFrame,
    y: np.ndarray,
    calibration_fraction: float = 0.20,
) -> dict[str, Any]:
    """Fit the selected estimator chronologically and calibrate on the newest block.

    Candidate selection must already have passed chronological CV. The latest
    calibration block is not used to fit the estimators. A future shadow period
    remains mandatory before any production promotion.
    """
    if len(X) != len(y) or y.ndim != 2 or y.shape[1] != 3:
        raise ValueError("Expected aligned Range features and [low, high, close] return targets")
    if len(X) < 150 or not 0.10 <= calibration_fraction <= 0.35:
        raise ValueError("Insufficient samples or invalid calibration fraction")
    if X.columns.duplicated().any() or X.isna().any().any():
        raise ValueError("Range training matrix has duplicate columns or missing values")
    if not np.isfinite(X.to_numpy(dtype=float)).all() or not np.isfinite(y).all():
        raise ValueError("Range training data contains non-finite values")
    # Every target row represents one real OHLC session relative to the
    # previous close. Reject impossible price geometry before fitting.
    if np.any(y[:, 0] <= -1.0) or np.any(y[:, 0] > y[:, 2]) or np.any(y[:, 2] > y[:, 1]):
        raise ValueError("Range targets must satisfy -100% < low_return <= close_return <= high_return")

    calibration_size = max(25, int(np.ceil(len(X) * calibration_fraction)))
    fit_end = len(X) - calibration_size
    if fit_end < 100:
        raise ValueError("Insufficient estimator-fit rows after calibration split")

    X_fit = X.iloc[:fit_end].copy()
    y_fit = y[:fit_end].copy()
    X_cal = X.iloc[fit_end:].copy()
    y_cal = y[fit_end:].copy()

    fit_scale = (
        _volatility_scale(X_fit)
        if candidate.get("volatility_scaled", False)
        else np.ones(len(X_fit), dtype=float)
    )
    bundle: dict[str, Any] = {
        "schema_version": 1,
        "candidate": candidate,
        "feature_names": list(X.columns),
        "fit_X": X_fit if candidate["family"] == "volatility_neighbours" else None,
        "fit_y_scaled": y_fit / fit_scale[:, None] if candidate["family"] == "volatility_neighbours" else None,
        "models": _fit_estimators(candidate, X_fit, y_fit),
        "normalized_adjustment": None,
        "calibration_alpha": ALPHA,
        "fit_samples": int(fit_end),
        "calibration_samples": int(calibration_size),
    }
    calibration_predictions = _predict_base(bundle, X_cal)
    bundle["normalized_adjustment"] = _conformal_adjustment(
        calibration_predictions, y_cal, X_cal, alpha=ALPHA
    )
    if not np.isfinite(bundle["normalized_adjustment"]):
        raise ValueError("Range conformal calibration produced a non-finite adjustment")
    return bundle


def predict_range_bundle(
    bundle: dict[str, Any], features: dict[str, float] | pd.DataFrame
) -> dict[str, float]:
    if bundle.get("schema_version") != 1:
        raise ValueError("Unsupported Range model bundle schema")
    if isinstance(features, pd.DataFrame):
        frame = features.copy()
    else:
        frame = pd.DataFrame([features])
    names = bundle["feature_names"]
    if not names or len(set(names)) != len(names):
        raise ValueError("Range bundle feature schema is empty or contains duplicates")
    if len(frame) != 1:
        raise ValueError("Range inference expects exactly one forecast-origin row")
    adjustment = bundle.get("normalized_adjustment")
    if adjustment is None or not np.isfinite(float(adjustment)) or float(adjustment) < 0.0:
        raise ValueError("Range bundle conformal adjustment must be finite and nonnegative")
    missing = [name for name in names if name not in frame.columns]
    if missing:
        raise ValueError("Range inference is missing model features: " + ", ".join(missing[:10]))
    frame = frame.loc[:, names].astype(float)
    if frame.isna().any().any() or not np.isfinite(frame.to_numpy()).all():
        raise ValueError("Range inference features contain missing or non-finite values")

    raw = _predict_base(bundle, frame)
    lower, upper, close = _base_bounds(raw)
    margin = float(bundle["normalized_adjustment"]) * _volatility_scale(frame)
    lower = np.minimum(lower - margin, close)
    upper = np.maximum(upper + margin, close)
    result = {
        "low_return": float(lower[0]),
        "high_return": float(upper[0]),
        "close_return": float(close[0]),
        "conformal_margin_return": float(margin[0]),
    }
    if not all(np.isfinite(value) for value in result.values()):
        raise ValueError("Range inference produced a non-finite forecast")
    if result["low_return"] >= result["high_return"]:
        raise ValueError("Range inference produced an invalid low/high interval")
    return result
