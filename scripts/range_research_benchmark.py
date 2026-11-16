from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
from sklearn.model_selection import TimeSeriesSplit
from sklearn.preprocessing import StandardScaler


ALPHA = 0.10
Q_LOW = 0.10
Q_HIGH = 0.90
RANDOM_STATE = 42


def _volatility_scale(X: pd.DataFrame) -> np.ndarray:
    """Forecast-origin-only scale; no target or future information is used."""
    candidates = []
    for column, weight in (
        ("realized_volatility_5_sessions", 1.0),
        ("realized_volatility_22_sessions", 0.75),
        ("latest__spot_intraday_range_pct", 0.50),
        ("mean5__spot_intraday_range_pct", 0.35),
    ):
        if column in X.columns:
            values = np.abs(X[column].to_numpy(dtype=float)) * weight
            candidates.append(values)
    if not candidates:
        raise ValueError("Volatility-scaled Range candidate needs rolling volatility features")
    scale = np.maximum.reduce(candidates)
    return np.clip(scale, 0.001, 0.06)


def _pinball(actual: np.ndarray, predicted: np.ndarray, quantile: float) -> float:
    error = actual - predicted
    return float(np.mean(np.maximum(quantile * error, (quantile - 1.0) * error)))


def _weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    order = np.argsort(values)
    ordered = values[order]
    ordered_weights = weights[order]
    cumulative = np.cumsum(ordered_weights)
    if cumulative[-1] <= 0:
        return float(np.quantile(values, q))
    position = q * cumulative[-1]
    index = min(int(np.searchsorted(cumulative, position, side="left")), len(ordered) - 1)
    return float(ordered[index])


def _volatility_neighbours(
    X_fit: pd.DataFrame, y_fit: np.ndarray, X_eval: pd.DataFrame, k: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    preferred = [
        "realized_volatility_5_sessions",
        "realized_volatility_22_sessions",
        "mean5__india_vix_close",
        "latest__spot_intraday_range_pct",
        "mean5__spot_intraday_range_pct",
        "std5__spot_return_1d",
    ]
    columns = [name for name in preferred if name in X_fit.columns]
    if len(columns) < 2:
        raise ValueError("Insufficient volatility-regime features for local quantile model")
    scaler = StandardScaler()
    fit_matrix = scaler.fit_transform(X_fit[columns].to_numpy(dtype=float))
    eval_matrix = scaler.transform(X_eval[columns].to_numpy(dtype=float))
    lower = np.empty(len(X_eval), dtype=float)
    upper = np.empty(len(X_eval), dtype=float)
    close = np.empty(len(X_eval), dtype=float)
    neighbours = min(max(12, int(k)), len(X_fit))
    for row_index, row in enumerate(eval_matrix):
        distances = np.sqrt(np.sum((fit_matrix - row) ** 2, axis=1))
        selected = np.argpartition(distances, neighbours - 1)[:neighbours]
        selected_distances = distances[selected]
        weights = 1.0 / np.maximum(selected_distances, 0.15)
        lower[row_index] = _weighted_quantile(y_fit[selected, 0], weights, Q_LOW)
        upper[row_index] = _weighted_quantile(y_fit[selected, 1], weights, Q_HIGH)
        close[row_index] = _weighted_quantile(y_fit[selected, 2], weights, 0.50)
    return lower, upper, close


def _make_sklearn_estimator(family: str, quantile: float | None, config: dict[str, Any]):
    if family == "hist_gradient_boosting":
        from sklearn.ensemble import HistGradientBoostingRegressor
        params = {
            "learning_rate": config["learning_rate"],
            "max_iter": config["iterations"],
            "max_leaf_nodes": config["max_leaf_nodes"],
            "min_samples_leaf": config["min_samples_leaf"],
            "l2_regularization": config["l2_regularization"],
            "early_stopping": False,
            "random_state": RANDOM_STATE,
        }
        if quantile is None:
            return HistGradientBoostingRegressor(loss="absolute_error", **params)
        return HistGradientBoostingRegressor(loss="quantile", quantile=quantile, **params)

    if family == "gradient_boosting":
        from sklearn.ensemble import GradientBoostingRegressor
        params = {
            "n_estimators": config["iterations"],
            "learning_rate": config["learning_rate"],
            "max_depth": config["max_depth"],
            "min_samples_leaf": config["min_samples_leaf"],
            "subsample": config["subsample"],
            "random_state": RANDOM_STATE,
            "loss": "absolute_error" if quantile is None else "quantile",
        }
        if quantile is not None:
            params["alpha"] = quantile
        return GradientBoostingRegressor(**params)

    if family in ("extra_trees_quantile", "random_forest_quantile"):
        from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor
        cls = ExtraTreesRegressor if family == "extra_trees_quantile" else RandomForestRegressor
        return cls(
            n_estimators=config["trees"],
            max_depth=config["max_depth"],
            min_samples_leaf=config["min_samples_leaf"],
            max_features=config["max_features"],
            bootstrap=(family == "random_forest_quantile"),
            n_jobs=2,
            random_state=RANDOM_STATE,
        )
    raise ValueError(f"Unsupported estimator family: {family}")


def _tree_distribution_prediction(model, X: pd.DataFrame, q: float) -> np.ndarray:
    # Forest estimators are fitted on sklearn's internal NumPy matrix. Passing
    # a DataFrame to every individual tree emits one warning per tree per
    # forecast and creates enormous CI logs during walk-forward refits.
    matrix = X.to_numpy(dtype=float, copy=False)
    tree_predictions = np.vstack([tree.predict(matrix) for tree in model.estimators_])
    return np.quantile(tree_predictions, q, axis=0)


def _fit_predict(
    candidate: dict[str, Any],
    X_fit: pd.DataFrame,
    y_fit: np.ndarray,
    X_eval: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    family = candidate["family"]
    config = candidate.get("config", {})
    scaled = bool(candidate.get("volatility_scaled", False))
    fit_scale = _volatility_scale(X_fit) if scaled else np.ones(len(X_fit), dtype=float)
    eval_scale = _volatility_scale(X_eval) if scaled else np.ones(len(X_eval), dtype=float)
    train_y = y_fit / fit_scale[:, None]

    if family == "volatility_neighbours":
        # Local conditional quantiles from earlier sessions in a similar
        # realized-volatility/VIX/intraday-range regime.
        result = _volatility_neighbours(X_fit, train_y, X_eval, int(config["k"]))
        return tuple(np.asarray(value) * eval_scale for value in result)

    predictions = []
    for target_column, q in ((0, Q_LOW), (1, Q_HIGH), (2, None)):
        model = _make_sklearn_estimator(family, q, config)
        model.fit(X_fit, train_y[:, target_column])
        if family in ("extra_trees_quantile", "random_forest_quantile"):
            level = 0.50 if q is None else q
            prediction = _tree_distribution_prediction(model, X_eval, level)
        else:
            prediction = model.predict(X_eval)
        predictions.append(np.asarray(prediction, dtype=float) * eval_scale)
    return predictions[0], predictions[1], predictions[2]


def _fit_predict_lightgbm(
    candidate: dict[str, Any],
    X_fit: pd.DataFrame,
    y_fit: np.ndarray,
    X_eval: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    from lightgbm import LGBMRegressor

    config = candidate["config"]
    scaled = bool(candidate.get("volatility_scaled", False))
    fit_scale = _volatility_scale(X_fit) if scaled else np.ones(len(X_fit), dtype=float)
    eval_scale = _volatility_scale(X_eval) if scaled else np.ones(len(X_eval), dtype=float)
    train_y = y_fit / fit_scale[:, None]
    predictions = []
    for target_column, q in ((0, Q_LOW), (1, Q_HIGH), (2, None)):
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
            "random_state": RANDOM_STATE,
            "n_jobs": 2,
        }
        if q is None:
            model = LGBMRegressor(objective="regression_l1", **params)
        else:
            model = LGBMRegressor(objective="quantile", alpha=q, **params)
        model.fit(X_fit, train_y[:, target_column])
        predictions.append(np.asarray(model.predict(X_eval), dtype=float) * eval_scale)
    return predictions[0], predictions[1], predictions[2]


def _predict_candidate(
    candidate: dict[str, Any],
    X_fit: pd.DataFrame,
    y_fit: np.ndarray,
    X_eval: pd.DataFrame,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    if candidate["family"] == "lightgbm_quantile":
        return _fit_predict_lightgbm(candidate, X_fit, y_fit, X_eval)
    return _fit_predict(candidate, X_fit, y_fit, X_eval)


def _base_bounds(
    predictions: tuple[np.ndarray, np.ndarray, np.ndarray]
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    low_raw, high_raw, close = predictions
    lower = np.minimum(low_raw, close)
    upper = np.maximum(high_raw, close)
    return lower, upper, close


def _conformal_adjustment(
    predictions: tuple[np.ndarray, np.ndarray, np.ndarray],
    actual: np.ndarray,
    X_calibration: pd.DataFrame,
    alpha: float = ALPHA,
) -> float:
    actual = np.asarray(actual, dtype=float)
    prediction_arrays = tuple(np.asarray(values, dtype=float).reshape(-1) for values in predictions)
    if actual.ndim != 2 or actual.shape[1] != 3:
        raise ValueError("Conformal calibration targets must have [low, high, close] columns")
    row_count = len(actual)
    if row_count == 0 or len(X_calibration) != row_count:
        raise ValueError("Conformal calibration features and targets must be non-empty and aligned")
    if any(len(values) != row_count for values in prediction_arrays):
        raise ValueError("Conformal calibration predictions are not aligned with targets")
    if not np.isfinite(actual).all() or any(not np.isfinite(values).all() for values in prediction_arrays):
        raise ValueError("Conformal calibration contains non-finite values")
    if np.any(actual[:, 0] <= -1.0) or np.any(actual[:, 0] > actual[:, 2]) or np.any(actual[:, 2] > actual[:, 1]):
        raise ValueError("Conformal calibration targets violate OHLC return ordering")
    if not 0.0 < alpha < 1.0:
        raise ValueError("Conformal calibration alpha must be between zero and one")
    lower, upper, _ = _base_bounds(prediction_arrays)
    # Normalize the joint nonconformity score by forecast-origin volatility.
    # This lets the calibration margin expand on volatile sessions and contract
    # on quiet sessions, using only information known at each forecast origin.
    calibration_scale = _volatility_scale(X_calibration)
    # Expansion-only interval calibration: covered calibration rows have a
    # zero nonconformity score. Negative scores would shrink the base interval
    # and can silently destroy the intended coverage guarantee.
    scores = np.maximum.reduce((
        lower - actual[:, 0],
        actual[:, 1] - upper,
        np.zeros(len(actual), dtype=float),
    )) / calibration_scale
    n = len(scores)
    if not np.isfinite(scores).all():
        raise ValueError("Invalid conformal calibration scores")
    rank = min(n, int(np.ceil((n + 1) * (1.0 - alpha))))
    return float(max(0.0, np.sort(scores)[max(0, rank - 1)]))


def _asymmetric_conformal_adjustment(
    predictions: tuple[np.ndarray, np.ndarray, np.ndarray],
    actual: np.ndarray,
    X_calibration: pd.DataFrame,
    alpha: float = ALPHA,
) -> dict[str, float]:
    """Calibrate each interval endpoint independently with Bonferroni coverage."""
    actual = np.asarray(actual, dtype=float)
    lower, upper, _ = _base_bounds(predictions)
    if actual.ndim != 2 or actual.shape[1] != 3:
        raise ValueError("Asymmetric calibration targets must have [low, high, close] columns")
    if len(actual) != len(X_calibration) or len(actual) == 0:
        raise ValueError("Asymmetric calibration features and targets must be aligned and non-empty")
    if not np.isfinite(actual).all() or not np.isfinite(lower).all() or not np.isfinite(upper).all():
        raise ValueError("Asymmetric calibration contains non-finite values")
    if not 0.0 < alpha < 1.0:
        raise ValueError("Asymmetric calibration alpha must be between zero and one")
    scale = _volatility_scale(X_calibration)
    low_scores = np.maximum(lower - actual[:, 0], 0.0) / scale
    high_scores = np.maximum(actual[:, 1] - upper, 0.0) / scale
    tail_alpha = alpha / 2.0
    rank = min(len(actual), int(np.ceil((len(actual) + 1) * (1.0 - tail_alpha))))
    return {
        "low": float(max(0.0, np.sort(low_scores)[max(0, rank - 1)])),
        "high": float(max(0.0, np.sort(high_scores)[max(0, rank - 1)])),
    }


def _range_metrics(
    predictions: tuple[np.ndarray, np.ndarray, np.ndarray],
    actual: np.ndarray,
    normalized_adjustment: float | dict[str, float],
    X_eval: pd.DataFrame,
) -> dict[str, float]:
    lower, upper, close = _base_bounds(predictions)
    scale = _volatility_scale(X_eval)
    if isinstance(normalized_adjustment, dict):
        low_adjustment = float(normalized_adjustment["low"]) * scale
        high_adjustment = float(normalized_adjustment["high"]) * scale
    else:
        low_adjustment = high_adjustment = float(normalized_adjustment) * scale
    lower = np.minimum(lower - low_adjustment, close)
    upper = np.maximum(upper + high_adjustment, close)
    low_actual, high_actual, close_actual = actual[:, 0], actual[:, 1], actual[:, 2]
    low_miss = np.maximum(lower - low_actual, 0.0)
    high_miss = np.maximum(high_actual - upper, 0.0)
    width = upper - lower
    winkler = width + (2.0 / ALPHA) * (low_miss + high_miss)
    return {
        "winkler_interval_score": float(np.mean(winkler)),
        "combined_pinball": _pinball(low_actual, lower, Q_LOW) + _pinball(high_actual, upper, Q_HIGH),
        "lower_pinball": _pinball(low_actual, lower, Q_LOW),
        "upper_pinball": _pinball(high_actual, upper, Q_HIGH),
        "low_return_mae": float(np.mean(np.abs(low_actual - lower))),
        "high_return_mae": float(np.mean(np.abs(high_actual - upper))),
        "close_return_mae": float(np.mean(np.abs(close_actual - close))),
        "mean_interval_width": float(np.mean(width)),
        "full_range_coverage": float(np.mean((low_actual >= lower) & (high_actual <= upper))),
        "low_endpoint_coverage": float(np.mean(low_actual >= lower)),
        "high_endpoint_coverage": float(np.mean(high_actual <= upper)),
        "close_inside_predicted_range_rate": float(np.mean((close_actual >= lower) & (close_actual <= upper))),
    }


def _baseline_predictions(
    y_fit: np.ndarray, length: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return (
        np.full(length, np.quantile(y_fit[:, 0], Q_LOW), dtype=float),
        np.full(length, np.quantile(y_fit[:, 1], Q_HIGH), dtype=float),
        np.full(length, np.median(y_fit[:, 2]), dtype=float),
    )


def _candidate_catalog() -> list[dict[str, Any]]:
    return [
        {"name": "volatility_neighbours_k40", "family": "volatility_neighbours", "config": {"k": 40}},
        {"name": "volatility_neighbours_k70", "family": "volatility_neighbours", "config": {"k": 70}},
        {"name": "lightgbm_quantile_compact", "family": "lightgbm_quantile",
         "config": {"n_estimators": 100, "learning_rate": 0.025, "num_leaves": 3, "max_depth": 2,
                   "min_child_samples": 25, "reg_lambda": 20.0, "reg_alpha": 3.0}},
        {"name": "lightgbm_quantile_vol_scaled", "family": "lightgbm_quantile", "volatility_scaled": True,
         "config": {"n_estimators": 120, "learning_rate": 0.02, "num_leaves": 5, "max_depth": 2,
                   "min_child_samples": 35, "reg_lambda": 30.0, "reg_alpha": 5.0}},
        {"name": "hist_gradient_boosting_shallow", "family": "hist_gradient_boosting",
         "config": {"iterations": 100, "learning_rate": 0.04, "max_leaf_nodes": 7,
                   "min_samples_leaf": 25, "l2_regularization": 5.0}},
        {"name": "hist_gradient_boosting_vol_scaled", "family": "hist_gradient_boosting", "volatility_scaled": True,
         "config": {"iterations": 120, "learning_rate": 0.03, "max_leaf_nodes": 7,
                   "min_samples_leaf": 30, "l2_regularization": 10.0}},
        {"name": "gradient_boosting_quantile", "family": "gradient_boosting",
         "config": {"iterations": 100, "learning_rate": 0.03, "max_depth": 1,
                   "min_samples_leaf": 20, "subsample": 0.85}},
        {"name": "extra_trees_conditional_quantile", "family": "extra_trees_quantile",
         "config": {"trees": 160, "max_depth": 5, "min_samples_leaf": 12, "max_features": 0.75}},
        {"name": "random_forest_conditional_quantile", "family": "random_forest_quantile",
         "config": {"trees": 160, "max_depth": 5, "min_samples_leaf": 12, "max_features": 0.75}},
    ]


def _mean_metrics(rows: list[dict[str, float]]) -> dict[str, float]:
    keys = rows[0].keys()
    return {key: float(np.mean([row[key] for row in rows])) for key in keys}


def tune_range_research(
    X: pd.DataFrame, y: np.ndarray, train_end: int
) -> dict[str, Any]:
    """Compare distinct range estimators using chronological CV and inner calibration.

    The final chronological segment is report-only. Candidate selection is confined
    to expanding-window CV inside the first 80% of all available samples.
    """
    if len(X) != len(y) or y.ndim != 2 or y.shape[1] != 3:
        raise ValueError("Range tuner expects aligned X and [next_low, next_high, next_close] targets")
    if train_end < 250 or len(X) - train_end < 30:
        raise ValueError(f"Insufficient chronological split: total={len(X)}, train={train_end}")
    X_train, X_hold = X.iloc[:train_end], X.iloc[train_end:]
    y_train, y_hold = y[:train_end], y[train_end:]
    splitter = TimeSeriesSplit(n_splits=3)
    folds = list(splitter.split(X_train))
    if len(folds) != 3:
        raise RuntimeError("Expected three expanding-window folds")

    baseline_fold_metrics: list[dict[str, float]] = []
    for fold_train, fold_valid in folds:
        calibration_size = max(18, int(np.ceil(len(fold_train) * 0.20)))
        if len(fold_train) - calibration_size < 55:
            raise RuntimeError("Not enough inner-fit samples for calibrated chronological CV")
        fit_idx, calibration_idx = fold_train[:-calibration_size], fold_train[-calibration_size:]
        base_cal = _baseline_predictions(y_train[fit_idx], len(calibration_idx))
        base_adjustment = _asymmetric_conformal_adjustment(base_cal, y_train[calibration_idx], X_train.iloc[calibration_idx])
        base_valid = _baseline_predictions(y_train[fit_idx], len(fold_valid))
        baseline_fold_metrics.append(
            _range_metrics(base_valid, y_train[fold_valid], base_adjustment, X_train.iloc[fold_valid])
        )
    baseline_cv = _mean_metrics(baseline_fold_metrics)

    candidate_rows: list[dict[str, Any]] = []
    for candidate in _candidate_catalog():
        fold_metrics: list[dict[str, float]] = []
        errors: list[str] = []
        try:
            for fold_train, fold_valid in folds:
                calibration_size = max(18, int(np.ceil(len(fold_train) * 0.20)))
                fit_idx, calibration_idx = fold_train[:-calibration_size], fold_train[-calibration_size:]
                fit_X = X_train.iloc[fit_idx]
                calibration_X = X_train.iloc[calibration_idx]
                valid_X = X_train.iloc[fold_valid]
                fit_y = y_train[fit_idx]
                calibration_y = y_train[calibration_idx]

                calibration_predictions = _predict_candidate(candidate, fit_X, fit_y, calibration_X)
                adjustment = _asymmetric_conformal_adjustment(calibration_predictions, calibration_y, calibration_X)
                valid_predictions = _predict_candidate(candidate, fit_X, fit_y, valid_X)
                fold_metrics.append(_range_metrics(valid_predictions, y_train[fold_valid], adjustment, valid_X))
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {str(exc)[:500]}")

        if len(fold_metrics) != len(folds):
            candidate_rows.append({
                "candidate": candidate,
                "status": "failed",
                "error": errors[-1] if errors else "Incomplete CV fold set",
                "completed_folds": len(fold_metrics),
            })
            continue

        mean_cv = _mean_metrics(fold_metrics)
        fold_wins = sum(
            1 for candidate_fold, baseline_fold in zip(fold_metrics, baseline_fold_metrics)
            if candidate_fold["winkler_interval_score"] < baseline_fold["winkler_interval_score"]
        )
        passes = (
            mean_cv["winkler_interval_score"] < baseline_cv["winkler_interval_score"]
            and mean_cv["combined_pinball"] <= baseline_cv["combined_pinball"] * 1.03
            and mean_cv["close_return_mae"] <= baseline_cv["close_return_mae"] * 1.10
            and mean_cv["full_range_coverage"] >= max(0.78, baseline_cv["full_range_coverage"] - 0.01)
            and min(metric["full_range_coverage"] for metric in fold_metrics) >= 0.70
            and fold_wins >= 2
        )
        candidate_rows.append({
            "candidate": candidate,
            "status": "evaluated",
            "cv": mean_cv,
            "fold_metrics": fold_metrics,
            "folds_won_on_winkler": fold_wins,
            "minimum_fold_full_range_coverage": float(
                min(metric["full_range_coverage"] for metric in fold_metrics)
            ),
            "cv_beats_baseline": bool(passes),
        })

    evaluated = [row for row in candidate_rows if row["status"] == "evaluated"]
    if not evaluated:
        raise RuntimeError("Every Range candidate failed; no result can be selected")
    feasible = [row for row in evaluated if row["cv_beats_baseline"]]
    # Prefer stable coverage before the smallest average score. A narrow model
    # that misses one tail repeatedly is not a useful next-day range forecast.
    robust_pool = [
        row for row in feasible
        if row["cv"]["full_range_coverage"] >= 0.93
        and row["minimum_fold_full_range_coverage"] >= 0.90
    ]
    selection_pool = robust_pool or feasible or evaluated
    selected = min(
        selection_pool,
        key=lambda row: (
            row["cv"]["winkler_interval_score"],
            -row["cv"]["full_range_coverage"],
            -row["minimum_fold_full_range_coverage"],
            row["cv"]["combined_pinball"],
            row["cv"]["close_return_mae"],
        ),
    )
    chosen = selected["candidate"]
    selection_policy = {
        "primary_metric": "mean chronological CV Winkler score (lower is better)",
        "robust_coverage_preference": "prefer candidates with mean CV full-range coverage >= 93% and minimum fold coverage >= 90%; if none qualify, fall back to all CV-feasible candidates",
        "selected_from_cv_only": True,
        "holdout_outcomes_used_for_selection": False,
        "robust_pool_size": len(robust_pool),
        "feasible_pool_size": len(feasible),
    }

    # Historical holdout remains diagnostic only; no candidate or calibration
    # choice is made from its outcomes.
    calibration_size = max(25, int(np.ceil(len(X_train) * 0.20)))
    if len(X_train) - calibration_size < 100:
        raise RuntimeError("Not enough training observations for final calibration split")
    final_fit_idx = np.arange(len(X_train) - calibration_size)
    final_cal_idx = np.arange(len(X_train) - calibration_size, len(X_train))
    final_fit_X = X_train.iloc[final_fit_idx]
    final_cal_X = X_train.iloc[final_cal_idx]
    final_fit_y = y_train[final_fit_idx]
    final_cal_y = y_train[final_cal_idx]

    base_cal = _baseline_predictions(final_fit_y, len(final_cal_idx))
    baseline_adjustment = _asymmetric_conformal_adjustment(base_cal, final_cal_y, final_cal_X)
    base_hold = _baseline_predictions(final_fit_y, len(X_hold))
    baseline_hold_metrics = _range_metrics(base_hold, y_hold, baseline_adjustment, X_hold)

    selected_cal = _predict_candidate(chosen, final_fit_X, final_fit_y, final_cal_X)
    selected_adjustment = _asymmetric_conformal_adjustment(selected_cal, final_cal_y, final_cal_X)
    selected_hold = _predict_candidate(chosen, final_fit_X, final_fit_y, X_hold)
    selected_hold_metrics = _range_metrics(selected_hold, y_hold, selected_adjustment, X_hold)

    return {
        "model": "Range-specialist benchmark: quantile models plus 90% joint conformal calibration",
        "selected_config": chosen,
        "selection_policy": selection_policy,
        "selected_cv": selected["cv"],
        "selected_minimum_fold_full_range_coverage": selected["minimum_fold_full_range_coverage"],
        "selected_cv_fold_metrics": selected["fold_metrics"],
        "selected_calibration_alpha": ALPHA,
        "selected_cv_combined_pinball": selected["cv"]["combined_pinball"],
        "baseline_cv_combined_pinball": baseline_cv["combined_pinball"],
        "cv_beats_baseline": bool(selected["cv_beats_baseline"]),
        "cv_full_range_coverage": selected["cv"]["full_range_coverage"],
        "baseline_cv": baseline_cv,
        "cv_winkler_improvement_pct": float(
            100.0 * (baseline_cv["winkler_interval_score"] - selected["cv"]["winkler_interval_score"])
            / max(abs(baseline_cv["winkler_interval_score"]), 1e-12)
        ),
        "candidate_results": candidate_rows,
        "holdout_diagnostic": selected_hold_metrics,
        "baseline_holdout_diagnostic": baseline_hold_metrics,
        "holdout_diagnostic_beats_baseline": bool(
            selected_hold_metrics["winkler_interval_score"] < baseline_hold_metrics["winkler_interval_score"]
        ),
        "holdout_is_selection_independent": False,
        "holdout_note": "This final 20% segment has been inspected in earlier research runs. It is diagnostic only, not an untouched test set.",
        "calibration": {
            "method": "asymmetric volatility-normalized split conformal calibration with Bonferroni endpoint allocation",
            "nominal_joint_full_range_coverage": 1.0 - ALPHA,
            "normalization": "separate lower- and upper-endpoint nonconformity scores divided by origin-session rolling volatility scale",
            "score": "lower=max(predicted_low-actual_low,0); upper=max(actual_high-predicted_high,0); each tail uses alpha/2",
            "fit_calibration_split": "first 80% fit, most recent 20% of each training fold calibrates; validation follows calibration chronologically",
        },
        "validation_policy": {
            "candidate_selection": "three expanding-window chronological folds within the first 80% of samples",
            "primary_metric": f"{1.0 - ALPHA:.0%} joint-range Winkler interval score (lower is better)",
            "guardrails": [
                "mean CV Winkler score beats the fold-matched calibrated quantile baseline",
                "mean CV combined pinball loss is no more than 3% worse than baseline",
                "mean CV close-return MAE is no more than 10% worse than baseline",
                "mean CV full-range coverage is at least 78% and no more than 1 percentage point below baseline",
                "no individual chronological fold has full-range coverage below 70%",
                "candidate wins Winkler score in at least two of three chronological folds",
            ],
            "all_transforms_fit_inside_fold": True,
            "minimum_full_range_coverage": 0.78,
            "future_data_used_for_features": False,
            "holdout_used_for_selection": False,
        },
        "promotion_gate": "CV must pass all four guardrails; passing this research benchmark still requires fresh forward shadow validation before model promotion.",
        "deployment_ready": False,
    }
