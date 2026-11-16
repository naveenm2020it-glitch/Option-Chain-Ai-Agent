from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import numpy as np
import pandas as pd

from scripts.train_nifty_four_models import (
    EXPECTED_SESSIONS,
    _build_daily_targets,
    _check_artifact_gates,
    _direction_metrics,
    _feature_matrix,
    build_rolling_feature_rows,
    build_seller_samples,
)


def _load_dependencies() -> None:
    global DummyClassifier, DummyRegressor, ExtraTreesRegressor, RandomForestRegressor
    global TimeSeriesSplit, f1_score, mean_absolute_error, r2_score
    from sklearn.dummy import DummyClassifier, DummyRegressor
    from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor
    from sklearn.metrics import f1_score, mean_absolute_error, r2_score
    from sklearn.model_selection import TimeSeriesSplit


def _direction_estimator(config: dict[str, Any]):
    from lightgbm import LGBMClassifier
    return LGBMClassifier(
        objective="multiclass",
        num_class=3,
        class_weight=config["class_weight"],
        n_estimators=config["n_estimators"],
        learning_rate=config["learning_rate"],
        num_leaves=config["num_leaves"],
        max_depth=config["max_depth"],
        min_child_samples=config["min_child_samples"],
        reg_lambda=config["reg_lambda"],
        reg_alpha=config["reg_alpha"],
        colsample_bytree=0.8,
        subsample=0.85,
        subsample_freq=1,
        verbosity=-1,
        random_state=42,
        n_jobs=2,
    )


def _aligned_probabilities(model, matrix: pd.DataFrame) -> np.ndarray:
    raw = model.predict_proba(matrix)
    aligned = np.zeros((len(matrix), 3), dtype=float)
    for column, label in enumerate(model.classes_):
        aligned[:, int(label)] = raw[:, column]
    return aligned


def _blend_probabilities(raw: np.ndarray, prior: np.ndarray, prior_weight: float) -> np.ndarray:
    return (1.0 - prior_weight) * raw + prior_weight * prior


def _pinball_loss(actual: np.ndarray, predicted: np.ndarray, quantile: float) -> float:
    error = actual - predicted
    return float(np.mean(np.maximum(quantile * error, (quantile - 1.0) * error)))


def _quantile_estimator(alpha: float, config: dict[str, Any]):
    from lightgbm import LGBMRegressor
    return LGBMRegressor(
        objective="quantile", alpha=alpha,
        n_estimators=config["n_estimators"],
        learning_rate=config["learning_rate"],
        num_leaves=config["num_leaves"],
        max_depth=config["max_depth"],
        min_child_samples=config["min_child_samples"],
        reg_lambda=config["reg_lambda"],
        reg_alpha=config["reg_alpha"],
        colsample_bytree=0.8, subsample=0.85, subsample_freq=1,
        verbosity=-1, random_state=42, n_jobs=2,
    )


def _close_estimator(config: dict[str, Any]):
    from lightgbm import LGBMRegressor
    return LGBMRegressor(
        objective="regression_l1",
        n_estimators=config["n_estimators"],
        learning_rate=config["learning_rate"],
        num_leaves=config["num_leaves"],
        max_depth=config["max_depth"],
        min_child_samples=config["min_child_samples"],
        reg_lambda=config["reg_lambda"],
        reg_alpha=config["reg_alpha"],
        colsample_bytree=0.8, subsample=0.85, subsample_freq=1,
        verbosity=-1, random_state=42, n_jobs=2,
    )


def _range_interval_metrics(
    actual: np.ndarray, lower_raw: np.ndarray, upper_raw: np.ndarray,
    close_pred: np.ndarray, q_low: float, q_high: float,
) -> dict[str, float]:
    low_actual, high_actual, close_actual = actual[:, 0], actual[:, 1], actual[:, 2]
    lower = np.minimum(lower_raw, close_pred)
    upper = np.maximum(upper_raw, close_pred)
    return {
        "lower_pinball": _pinball_loss(low_actual, lower_raw, q_low),
        "upper_pinball": _pinball_loss(high_actual, upper_raw, q_high),
        "combined_pinball": _pinball_loss(low_actual, lower_raw, q_low) + _pinball_loss(high_actual, upper_raw, q_high),
        "mean_interval_width": float(np.mean(upper - lower)),
        "low_containment_rate": float(np.mean(low_actual >= lower)),
        "high_containment_rate": float(np.mean(high_actual <= upper)),
        "full_range_coverage": float(np.mean((low_actual >= lower) & (high_actual <= upper))),
        "close_inside_predicted_range_rate": float(np.mean((close_actual >= lower) & (close_actual <= upper))),
        "close_return_mae": float(mean_absolute_error(close_actual, close_pred)),
    }


def _fit_range_quantiles(
    X_train: pd.DataFrame, y_train: np.ndarray, X_eval: pd.DataFrame,
    q_low: float, q_high: float, config: dict[str, Any],
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    lower_model = _quantile_estimator(q_low, config).fit(X_train, y_train[:, 0])
    upper_model = _quantile_estimator(q_high, config).fit(X_train, y_train[:, 1])
    close_model = _close_estimator(config).fit(X_train, y_train[:, 2])
    return (
        np.asarray(lower_model.predict(X_eval), dtype=float),
        np.asarray(upper_model.predict(X_eval), dtype=float),
        np.asarray(close_model.predict(X_eval), dtype=float),
    )


def _constant_range_quantiles(
    y_train: np.ndarray, length: int, q_low: float, q_high: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return (
        np.full(length, np.quantile(y_train[:, 0], q_low), dtype=float),
        np.full(length, np.quantile(y_train[:, 1], q_high), dtype=float),
        np.full(length, np.median(y_train[:, 2]), dtype=float),
    )


def _seller_metrics(actual: np.ndarray, predicted: np.ndarray, dates: list[str]) -> dict[str, float]:
    errors = np.abs(actual - predicted)
    by_date = pd.DataFrame({"date": dates, "error": errors}).groupby("date")["error"].mean()
    return {
        "pooled_mae": float(errors.mean()),
        "date_balanced_mae": float(by_date.mean()),
        "holdout_dates": int(by_date.shape[0]),
        "holdout_contract_rows": int(len(actual)),
        "r2": float(r2_score(actual, predicted)),
        "positive_decay_rate": float(np.mean(actual > 0)),
    }


def _date_folds(dates: list[str], n_splits: int = 3):
    unique_dates = np.asarray(sorted(set(dates)))
    splitter = TimeSeriesSplit(n_splits=n_splits)
    for train_i, valid_i in splitter.split(unique_dates):
        yield set(unique_dates[train_i]), set(unique_dates[valid_i])


def tune_direction(X: pd.DataFrame, close_returns: np.ndarray, train_end: int) -> dict[str, Any]:
    X_train, X_hold = X.iloc[:train_end], X.iloc[train_end:]
    returns_train, returns_hold = close_returns[:train_end], close_returns[train_end:]
    configs = [
        {"name": "lgbm_shallow_unweighted", "class_weight": None, "n_estimators": 100, "learning_rate": 0.025, "num_leaves": 3, "max_depth": 2, "min_child_samples": 25, "reg_lambda": 20.0, "reg_alpha": 3.0},
        {"name": "lgbm_shallow_balanced", "class_weight": "balanced", "n_estimators": 100, "learning_rate": 0.025, "num_leaves": 3, "max_depth": 2, "min_child_samples": 25, "reg_lambda": 20.0, "reg_alpha": 3.0},
        {"name": "lgbm_compact_unweighted", "class_weight": None, "n_estimators": 140, "learning_rate": 0.025, "num_leaves": 5, "max_depth": 2, "min_child_samples": 35, "reg_lambda": 20.0, "reg_alpha": 3.0},
        {"name": "lgbm_compact_balanced", "class_weight": "balanced", "n_estimators": 140, "learning_rate": 0.025, "num_leaves": 5, "max_depth": 2, "min_child_samples": 35, "reg_lambda": 20.0, "reg_alpha": 3.0},
        {"name": "lgbm_regularized", "class_weight": None, "n_estimators": 180, "learning_rate": 0.02, "num_leaves": 7, "max_depth": 3, "min_child_samples": 45, "reg_lambda": 30.0, "reg_alpha": 5.0},
        {"name": "lgbm_regularized_balanced", "class_weight": "balanced", "n_estimators": 180, "learning_rate": 0.02, "num_leaves": 7, "max_depth": 3, "min_child_samples": 45, "reg_lambda": 30.0, "reg_alpha": 5.0},
    ]
    blends = (0.0, 0.2, 0.4, 0.6)
    thresholds = (0.0015, 0.0025, 0.0030, 0.0035, 0.0040)
    splitter = TimeSeriesSplit(n_splits=3)
    threshold_results: list[dict[str, Any]] = []

    for threshold in thresholds:
        y_train = np.where(returns_train > threshold, 2, np.where(returns_train < -threshold, 0, 1)).astype(int)
        y_hold = np.where(returns_hold > threshold, 2, np.where(returns_hold < -threshold, 0, 1)).astype(int)
        baseline_folds = []
        for tr, va in splitter.split(X_train):
            if len(np.unique(y_train[tr])) < 3:
                continue
            baseline = DummyClassifier(strategy="prior").fit(X_train.iloc[tr], y_train[tr])
            raw = _aligned_probabilities(baseline, X_train.iloc[va])
            metrics = _direction_metrics(y_train[va], raw)
            baseline_folds.append(metrics)
        baseline_cv = {
            key: float(np.mean([row[key] for row in baseline_folds]))
            for key in ("macro_f1", "multiclass_brier", "accuracy")
        }
        candidates: list[dict[str, Any]] = []
        for config in configs:
            fold_raw: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
            for tr, va in splitter.split(X_train):
                if len(np.unique(y_train[tr])) < 3:
                    continue
                model = _direction_estimator(config).fit(X_train.iloc[tr], y_train[tr])
                raw = _aligned_probabilities(model, X_train.iloc[va])
                prior = np.bincount(y_train[tr], minlength=3).astype(float) / len(tr)
                fold_raw.append((raw, prior, y_train[va]))
            for blend in blends:
                fold_metrics = [
                    _direction_metrics(actual, _blend_probabilities(raw, prior, blend))
                    for raw, prior, actual in fold_raw
                ]
                avg = {
                    key: float(np.mean([row[key] for row in fold_metrics]))
                    for key in ("macro_f1", "multiclass_brier", "accuracy")
                }
                candidates.append({
                    "config": config, "prior_blend": blend, "cv": avg,
                    "cv_score": avg["macro_f1"] - 0.25 * avg["multiclass_brier"],
                    "cv_beats_baseline": (
                        avg["macro_f1"] > baseline_cv["macro_f1"]
                        and avg["multiclass_brier"] < baseline_cv["multiclass_brier"]
                    ),
                })
        feasible = [item for item in candidates if item["cv_beats_baseline"]]
        pool = feasible if feasible else candidates
        selected = max(pool, key=lambda item: item["cv_score"])
        estimator = _direction_estimator(selected["config"]).fit(X_train, y_train)
        prior = np.bincount(y_train, minlength=3).astype(float) / len(y_train)
        hold_prob = _blend_probabilities(
            _aligned_probabilities(estimator, X_hold), prior, selected["prior_blend"]
        )
        hold_metrics = _direction_metrics(y_hold, hold_prob)
        baseline = DummyClassifier(strategy="prior").fit(X_train, y_train)
        baseline_hold = _direction_metrics(y_hold, _aligned_probabilities(baseline, X_hold))
        hold_beats = (
            hold_metrics["macro_f1"] > baseline_hold["macro_f1"]
            and hold_metrics["multiclass_brier"] < baseline_hold["multiclass_brier"]
        )
        threshold_results.append({
            "threshold": threshold,
            "selected_config": selected["config"],
            "selected_prior_blend": selected["prior_blend"],
            "selected_cv": selected["cv"],
            "baseline_cv": baseline_cv,
            "cv_feasible_candidate_count": len(feasible),
            "candidate_count": len(candidates),
            "holdout_diagnostic": hold_metrics,
            "baseline_holdout_diagnostic": baseline_hold,
            "holdout_diagnostic_beats_baseline": hold_beats,
            "cv_beats_baseline": selected["cv_beats_baseline"],
        })

    cv_passes = [item for item in threshold_results if item["cv_beats_baseline"]]
    selection_pool = cv_passes if cv_passes else threshold_results
    selected_threshold = max(
        selection_pool,
        key=lambda item: item["selected_cv"]["macro_f1"] - 0.25 * item["selected_cv"]["multiclass_brier"],
    )
    return {
        "selected_threshold": selected_threshold["threshold"],
        "selected_config": selected_threshold["selected_config"],
        "selected_prior_blend": selected_threshold["selected_prior_blend"],
        "selected_cv": selected_threshold["selected_cv"],
        "baseline_cv": selected_threshold["baseline_cv"],
        "cv_beats_baseline": selected_threshold["cv_beats_baseline"],
        "holdout_diagnostic": selected_threshold["holdout_diagnostic"],
        "baseline_holdout_diagnostic": selected_threshold["baseline_holdout_diagnostic"],
        "holdout_diagnostic_beats_baseline": selected_threshold["holdout_diagnostic_beats_baseline"],
        "threshold_candidates": threshold_results,
        "selection_policy": "direction threshold, model hyperparameters and prior blend selected only on expanding-window CV within the first 80%; final 20% shown as a previously reused diagnostic, not an untouched test",
        "promotion_gate": "CV macro-F1 must exceed prior baseline and CV Brier must be lower; holdout diagnostic is informational only",
        "beats_baseline": selected_threshold["cv_beats_baseline"],
    }


def tune_range(X: pd.DataFrame, y: np.ndarray, train_end: int) -> dict[str, Any]:
    X_train, X_hold = X.iloc[:train_end], X.iloc[train_end:]
    y_train, y_hold = y[:train_end], y[train_end:]
    configs = [
        {"name": "quantile_compact", "n_estimators": 100, "learning_rate": 0.025, "num_leaves": 3, "max_depth": 2, "min_child_samples": 25, "reg_lambda": 20.0, "reg_alpha": 3.0},
        {"name": "quantile_regularized", "n_estimators": 140, "learning_rate": 0.02, "num_leaves": 5, "max_depth": 2, "min_child_samples": 35, "reg_lambda": 30.0, "reg_alpha": 5.0},
    ]
    quantiles = ((0.05, 0.95), (0.10, 0.90), (0.15, 0.85))
    candidate_rows = []
    splitter = TimeSeriesSplit(n_splits=3)

    for q_low, q_high in quantiles:
        base_fold_scores = []
        for tr, va in splitter.split(X_train):
            lo, hi, _ = _constant_range_quantiles(y_train[tr], len(va), q_low, q_high)
            baseline_metrics = _range_interval_metrics(y_train[va], lo, hi, np.full(len(va), np.median(y_train[tr, 2])), q_low, q_high)
            base_fold_scores.append(baseline_metrics["combined_pinball"])
        baseline_cv = float(np.mean(base_fold_scores))
        for config in configs:
            fold_scores = []
            fold_coverages = []
            for tr, va in splitter.split(X_train):
                lower, upper, close = _fit_range_quantiles(
                    X_train.iloc[tr], y_train[tr], X_train.iloc[va], q_low, q_high, config
                )
                metrics = _range_interval_metrics(y_train[va], lower, upper, close, q_low, q_high)
                fold_scores.append(metrics["combined_pinball"])
                fold_coverages.append(metrics["full_range_coverage"])
            candidate_rows.append({
                "config": config, "q_low": q_low, "q_high": q_high,
                "cv_combined_pinball": float(np.mean(fold_scores)),
                "cv_full_range_coverage": float(np.mean(fold_coverages)),
                "baseline_cv_combined_pinball": baseline_cv,
                "cv_beats_baseline": float(np.mean(fold_scores)) < baseline_cv,
            })
    feasible = [item for item in candidate_rows if item["cv_beats_baseline"]]
    pool = feasible if feasible else candidate_rows
    selected = min(pool, key=lambda item: item["cv_combined_pinball"])
    lower, upper, close = _fit_range_quantiles(
        X_train, y_train, X_hold, selected["q_low"], selected["q_high"], selected["config"]
    )
    hold_metrics = _range_interval_metrics(
        y_hold, lower, upper, close, selected["q_low"], selected["q_high"]
    )
    base_lower, base_upper, base_close = _constant_range_quantiles(
        y_train, len(X_hold), selected["q_low"], selected["q_high"]
    )
    baseline_metrics = _range_interval_metrics(
        y_hold, base_lower, base_upper, base_close, selected["q_low"], selected["q_high"]
    )
    hold_beats = hold_metrics["combined_pinball"] < baseline_metrics["combined_pinball"]
    return {
        "model": "three LightGBM heads within one Range Intelligence group: lower quantile of next low, upper quantile of next high, and median next close",
        "selected_config": selected["config"],
        "selected_quantiles": {"low": selected["q_low"], "high": selected["q_high"]},
        "selected_cv_combined_pinball": selected["cv_combined_pinball"],
        "baseline_cv_combined_pinball": selected["baseline_cv_combined_pinball"],
        "cv_beats_baseline": selected["cv_beats_baseline"],
        "cv_full_range_coverage": selected["cv_full_range_coverage"],
        "candidate_results": candidate_rows,
        "holdout_diagnostic": hold_metrics,
        "baseline_holdout_diagnostic": baseline_metrics,
        "holdout_diagnostic_beats_baseline": hold_beats,
        "beats_baseline": selected["cv_beats_baseline"],
        "selection_policy": "quantile levels and estimator configuration selected only by expanding-window training CV pinball loss; final 20% reported as a previously reused diagnostic",
    }


def _seller_estimator(config: dict[str, Any]):
    from lightgbm import LGBMRegressor
    return LGBMRegressor(
        objective="regression_l1",
        n_estimators=config["n_estimators"],
        learning_rate=0.025,
        num_leaves=config["num_leaves"],
        max_depth=config["max_depth"],
        min_child_samples=config["min_child_samples"],
        reg_lambda=config["reg_lambda"],
        reg_alpha=config["reg_alpha"],
        colsample_bytree=0.8,
        subsample=0.85,
        subsample_freq=1,
        verbosity=-1,
        random_state=42,
        n_jobs=2,
    )


def tune_seller(side: str, X: pd.DataFrame, y: np.ndarray, dates: list[str]) -> dict[str, Any]:
    unique_dates = sorted(set(dates))
    cutoff = max(1, int(len(unique_dates) * 0.8))
    cutoff_date = unique_dates[cutoff]
    train_mask = np.asarray([d < cutoff_date for d in dates], dtype=bool)
    hold_mask = ~train_mask
    X_train, X_hold = X.loc[train_mask], X.loc[hold_mask]
    y_train, y_hold = y[train_mask], y[hold_mask]
    train_dates = [d for d, ok in zip(dates, train_mask) if ok]
    hold_dates = [d for d, ok in zip(dates, hold_mask) if ok]
    configs = [
        {"name": "lgbm_current_compact", "n_estimators": 130, "num_leaves": 7, "max_depth": 3, "min_child_samples": 60, "reg_lambda": 15.0, "reg_alpha": 2.0},
        {"name": "lgbm_more_regularized", "n_estimators": 100, "num_leaves": 5, "max_depth": 2, "min_child_samples": 100, "reg_lambda": 30.0, "reg_alpha": 5.0},
        {"name": "lgbm_smoother", "n_estimators": 160, "num_leaves": 9, "max_depth": 3, "min_child_samples": 100, "reg_lambda": 25.0, "reg_alpha": 4.0},
    ]
    baseline_fold_pool, baseline_fold_date = [], []
    for fold_train_dates, fold_valid_dates in _date_folds(train_dates):
        row_dates = np.asarray(train_dates)
        tr = np.isin(row_dates, list(fold_train_dates))
        va = np.isin(row_dates, list(fold_valid_dates))
        baseline = DummyRegressor(strategy="median").fit(X_train.iloc[tr], y_train[tr])
        pred = baseline.predict(X_train.iloc[va])
        actual = y_train[va]
        eval_dates = np.asarray(train_dates)[va].tolist()
        metrics = _seller_metrics(actual, pred, eval_dates)
        baseline_fold_pool.append(metrics["pooled_mae"])
        baseline_fold_date.append(metrics["date_balanced_mae"])
    if not baseline_fold_pool:
        raise RuntimeError(f"{side} seller tuning could not form date-wise CV folds")
    candidate_rows = []
    for config in configs:
        pooled, date_balanced = [], []
        for fold_train_dates, fold_valid_dates in _date_folds(train_dates):
            row_dates = np.asarray(train_dates)
            tr = np.isin(row_dates, list(fold_train_dates))
            va = np.isin(row_dates, list(fold_valid_dates))
            model = _seller_estimator(config)
            model.fit(X_train.iloc[tr], y_train[tr])
            pred = model.predict(X_train.iloc[va])
            eval_dates = np.asarray(train_dates)[va].tolist()
            metrics = _seller_metrics(y_train[va], pred, eval_dates)
            pooled.append(metrics["pooled_mae"])
            date_balanced.append(metrics["date_balanced_mae"])
        candidate_rows.append({
            "config": config,
            "cv_pooled_mae": float(np.mean(pooled)),
            "cv_date_balanced_mae": float(np.mean(date_balanced)),
        })
    selected = min(candidate_rows, key=lambda item: item["cv_date_balanced_mae"])
    final_model = _seller_estimator(selected["config"])
    final_model.fit(X_train, y_train)
    prediction = final_model.predict(X_hold)
    baseline = DummyRegressor(strategy="median").fit(X_train, y_train)
    base_prediction = baseline.predict(X_hold)
    hold_metrics = _seller_metrics(y_hold, prediction, hold_dates)
    base_metrics = _seller_metrics(y_hold, base_prediction, hold_dates)

    # Date-wise stability: every trading date receives equal weight, regardless
    # of how many contracts happened to be listed on that date.
    date_frame = pd.DataFrame({
        "date": hold_dates,
        "actual": y_hold,
        "model_prediction": prediction,
        "baseline_prediction": base_prediction,
    })
    date_frame["model_abs_error"] = np.abs(date_frame["actual"] - date_frame["model_prediction"])
    date_frame["baseline_abs_error"] = np.abs(date_frame["actual"] - date_frame["baseline_prediction"])
    daily = date_frame.groupby("date", sort=True).agg(
        model_mae=("model_abs_error", "mean"),
        baseline_mae=("baseline_abs_error", "mean"),
        contract_rows=("actual", "size"),
    )
    daily["model_beats_baseline"] = daily["model_mae"] < daily["baseline_mae"]
    date_stability = {
        "holdout_dates": int(len(daily)),
        "model_wins_dates": int(daily["model_beats_baseline"].sum()),
        "date_win_rate": float(daily["model_beats_baseline"].mean()),
        "median_daily_model_mae": float(daily["model_mae"].median()),
        "median_daily_baseline_mae": float(daily["baseline_mae"].median()),
        "p25_daily_model_mae": float(daily["model_mae"].quantile(0.25)),
        "p75_daily_model_mae": float(daily["model_mae"].quantile(0.75)),
        "worst_daily_model_mae": float(daily["model_mae"].max()),
        "date_metrics": [
            {"date": str(day), "model_mae": float(row.model_mae),
             "baseline_mae": float(row.baseline_mae), "contract_rows": int(row.contract_rows)}
            for day, row in daily.iterrows()
        ],
        "policy": "Equal-weight date-level comparison; contracts within a date are averaged before dates are compared.",
    }
    cv_beats = (
        selected["cv_pooled_mae"] < float(np.mean(baseline_fold_pool))
        and selected["cv_date_balanced_mae"] < float(np.mean(baseline_fold_date))
    )
    holdout_beats = (
        hold_metrics["pooled_mae"] < base_metrics["pooled_mae"]
        and hold_metrics["date_balanced_mae"] < base_metrics["date_balanced_mae"]
    )
    beats = cv_beats and holdout_beats
    return {
        "selected_config": selected["config"],
        "selected_cv_pooled_mae": selected["cv_pooled_mae"],
        "selected_cv_date_balanced_mae": selected["cv_date_balanced_mae"],
        "baseline_cv_pooled_mae": float(np.mean(baseline_fold_pool)),
        "baseline_cv_date_balanced_mae": float(np.mean(baseline_fold_date)),
        "candidate_results": candidate_rows,
        "holdout": hold_metrics,
        "baseline_holdout": base_metrics,
        "date_stability": date_stability,
        "date_stability_pass": (
            date_stability["date_win_rate"] > 0.5
            and hold_metrics["date_balanced_mae"] < base_metrics["date_balanced_mae"]
        ),
        "cv_beats_baseline": cv_beats,
        "holdout_diagnostic_beats_baseline": holdout_beats,
        "beats_baseline": beats and date_stability["date_win_rate"] > 0.5,
        "holdout_start": cutoff_date,
        "holdout_end": max(hold_dates),
        "split_policy": "chronological split and three expanding-window folds by trading date; no random contract-row split",
        "promotion_policy": "requires both training-only date-grouped CV and the reused historical holdout diagnostic to beat the baseline on pooled and date-balanced MAE",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Tune four NIFTY research models using training-only walk-forward CV.")
    parser.add_argument("--checkpoint", type=Path, default=Path("data/historical_audit/checkpoint.json"))
    parser.add_argument("--audit-report", type=Path, default=Path("data/historical_audit/coverage.json"))
    parser.add_argument("--repair-report", type=Path, default=Path("data/context_repair/context_repair_report.json"))
    parser.add_argument("--output", type=Path, default=Path("models/tuning_artifacts/four_model_tuning_report.json"))
    args = parser.parse_args()

    _load_dependencies()
    checkpoint, audit, repair = _check_artifact_gates(args.checkpoint, args.audit_report, args.repair_report)
    feature_rows = checkpoint["features"]
    if len(feature_rows) != EXPECTED_SESSIONS:
        raise RuntimeError("Tuning requires the fully verified 494-session archive")
    rolling = build_rolling_feature_rows(feature_rows)
    X_daily, feature_names = _feature_matrix(rolling)
    targets = _build_daily_targets(rolling, feature_rows)
    split = int(len(X_daily) * 0.8)
    if split < 250 or len(X_daily) - split < 50:
        raise RuntimeError("Chronological daily train/holdout split is too small")

    range_targets = np.column_stack((
        targets["low_return"], targets["high_return"], targets["close_return"]
    ))
    daily_result = {
        "train_samples": split,
        "locked_holdout_samples": len(X_daily) - split,
        "origin_start": rolling[0]["origin_date"],
        "origin_end": rolling[-1]["origin_date"],
        "feature_count": len(feature_names),
        "direction": tune_direction(X_daily, targets["close_return"], split),
        "range": tune_range(X_daily, range_targets, split),
    }

    seller_results = {}
    for side in ("CE", "PE"):
        print(f"Tuning {side} seller model with trading-date grouped folds...", flush=True)
        X_seller, y_seller, _, seller_dates = build_seller_samples(feature_rows, rolling, side)
        seller_results[side] = tune_seller(side, X_seller, y_seller, seller_dates)

    checks = {
        "direction": daily_result["direction"]["beats_baseline"],
        "range": daily_result["range"]["beats_baseline"],
        "CE_seller": seller_results["CE"]["beats_baseline"],
        "PE_seller": seller_results["PE"]["beats_baseline"],
    }
    report = {
        "status": "tuning_evaluation_complete",
        "model_group_count": 4,
        "training_archive_sessions": len(feature_rows),
        "daily_rolling_feature_count": len(feature_names),
        "rolling_windows": {"recent_sessions": 5, "monthly_sessions": 22, "both_include_latest_completed_origin_session": True},
        "tuning_method": "chronological expanding-window validation; direction threshold/hyperparameters, range quantiles/config and seller hyperparameters selected only on training portion",
        "historical_holdout_policy": "The final 20 percent has been inspected in prior training/tuning runs. It is reported only as a diagnostic and is not treated as an untouched independent test. Promotion gates rely on training-only walk-forward CV plus future shadow results.",
        "seller_validation_method": "grouped by trading date, not random contract rows; reports pooled and equal-weight per-date MAE; must beat baseline on both measures in both training CV and historical holdout diagnostic",
        "range_validation_method": "quantile pinball loss for predicted low/high bands, with interval coverage and width reported; quantile constants form the naive baseline",
        "missing_value_policy": "strict; source gaps block tuning, no imputation",
        "input_dataset_sha256": str(repair.get("checkpoint_sha256", "")),
        "b2_reads": 0,
        "b2_writes": 0,
        "archive_gates": {
            "audit_training_allowed": audit.get("model_training_allowed"),
            "repair_training_allowed": repair.get("training_allowed"),
            "date_alignment_pass": repair.get("date_alignment_pass"),
            "feature_validation_pass": repair.get("feature_validation", {}).get("pass"),
            "missing_context_dates": repair.get("missing_context_dates"),
        },
        "daily": daily_result,
        "seller": seller_results,
        "baseline_checks": checks,
        "research_validation_pass": all(checks.values()),
        "deployment_ready": False,
        "promotion_note": "This is a tuning report only. It does not replace production artifacts or change the Render app. Promote a tuned candidate only after it beats its naive baseline on the locked chronological holdout and passes subsequent shadow validation.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
