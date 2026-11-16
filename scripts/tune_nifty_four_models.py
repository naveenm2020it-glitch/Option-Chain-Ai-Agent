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


def _range_prediction(model, matrix: pd.DataFrame) -> np.ndarray:
    prediction = np.asarray(model.predict(matrix), dtype=float)
    # Preserve the physical relation low <= close <= high in the published range.
    prediction[:, 0] = np.minimum(prediction[:, 0], prediction[:, 2])
    prediction[:, 1] = np.maximum(prediction[:, 1], prediction[:, 2])
    return prediction


def _range_metrics(actual: np.ndarray, predicted: np.ndarray) -> dict[str, float]:
    return {
        "combined_mae": float(np.mean(np.abs(actual - predicted))),
        "low_return_mae": float(mean_absolute_error(actual[:, 0], predicted[:, 0])),
        "high_return_mae": float(mean_absolute_error(actual[:, 1], predicted[:, 1])),
        "close_return_mae": float(mean_absolute_error(actual[:, 2], predicted[:, 2])),
        "close_inside_predicted_range_rate": float(np.mean(
            (actual[:, 2] >= predicted[:, 0]) & (actual[:, 2] <= predicted[:, 1])
        )),
    }


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


def tune_direction(X: pd.DataFrame, y: np.ndarray, train_end: int) -> dict[str, Any]:
    X_train, X_hold = X.iloc[:train_end], X.iloc[train_end:]
    y_train, y_hold = y[:train_end], y[train_end:]
    configs = [
        {"name": "lgbm_shallow_unweighted", "class_weight": None, "n_estimators": 100, "learning_rate": 0.025, "num_leaves": 3, "max_depth": 2, "min_child_samples": 25, "reg_lambda": 20.0, "reg_alpha": 3.0},
        {"name": "lgbm_shallow_balanced", "class_weight": "balanced", "n_estimators": 100, "learning_rate": 0.025, "num_leaves": 3, "max_depth": 2, "min_child_samples": 25, "reg_lambda": 20.0, "reg_alpha": 3.0},
        {"name": "lgbm_compact_unweighted", "class_weight": None, "n_estimators": 140, "learning_rate": 0.025, "num_leaves": 5, "max_depth": 2, "min_child_samples": 35, "reg_lambda": 20.0, "reg_alpha": 3.0},
        {"name": "lgbm_compact_balanced", "class_weight": "balanced", "n_estimators": 140, "learning_rate": 0.025, "num_leaves": 5, "max_depth": 2, "min_child_samples": 35, "reg_lambda": 20.0, "reg_alpha": 3.0},
        {"name": "lgbm_regularized", "class_weight": None, "n_estimators": 180, "learning_rate": 0.02, "num_leaves": 7, "max_depth": 3, "min_child_samples": 45, "reg_lambda": 30.0, "reg_alpha": 5.0},
        {"name": "lgbm_regularized_balanced", "class_weight": "balanced", "n_estimators": 180, "learning_rate": 0.02, "num_leaves": 7, "max_depth": 3, "min_child_samples": 45, "reg_lambda": 30.0, "reg_alpha": 5.0},
    ]
    blends = (0.0, 0.2, 0.4, 0.6)
    results: list[dict[str, Any]] = []
    splitter = TimeSeriesSplit(n_splits=3)
    baseline_folds = []
    for tr, va in splitter.split(X_train):
        if len(np.unique(y_train[tr])) < 3:
            continue
        baseline = DummyClassifier(strategy="prior").fit(X_train.iloc[tr], y_train[tr])
        raw = _aligned_probabilities(baseline, X_train.iloc[va])
        prior = np.bincount(y_train[tr], minlength=3).astype(float) / len(tr)
        metrics = _direction_metrics(y_train[va], _blend_probabilities(raw, prior, 0.0))
        baseline_folds.append(metrics)
    baseline_cv = {
        key: float(np.mean([row[key] for row in baseline_folds]))
        for key in ("macro_f1", "multiclass_brier", "accuracy")
    }
    for config in configs:
        fold_raw: list[tuple[np.ndarray, np.ndarray, np.ndarray]] = []
        for tr, va in splitter.split(X_train):
            if len(np.unique(y_train[tr])) < 3:
                continue
            model = _direction_estimator(config)
            model.fit(X_train.iloc[tr], y_train[tr])
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
            results.append({
                "config": config,
                "prior_blend": blend,
                "cv": avg,
                "cv_score": avg["macro_f1"] - 0.25 * avg["multiclass_brier"],
            })
    if not results:
        raise RuntimeError("Direction tuning could not form valid chronological folds")
    selected = max(results, key=lambda item: item["cv_score"])
    estimator = _direction_estimator(selected["config"])
    estimator.fit(X_train, y_train)
    prior = np.bincount(y_train, minlength=3).astype(float) / len(y_train)
    hold_prob = _blend_probabilities(
        _aligned_probabilities(estimator, X_hold), prior, selected["prior_blend"]
    )
    hold_metrics = _direction_metrics(y_hold, hold_prob)
    baseline = DummyClassifier(strategy="prior").fit(X_train, y_train)
    base_metrics = _direction_metrics(y_hold, _aligned_probabilities(baseline, X_hold))
    beats = (
        hold_metrics["macro_f1"] > base_metrics["macro_f1"]
        and hold_metrics["multiclass_brier"] < base_metrics["multiclass_brier"]
    )
    return {
        "selected_config": selected["config"],
        "selected_prior_blend": selected["prior_blend"],
        "selected_cv": selected["cv"],
        "baseline_cv": baseline_cv,
        "candidate_count": len(results),
        "holdout": hold_metrics,
        "baseline_holdout": base_metrics,
        "beats_baseline": beats,
        "selection_policy": "training-only chronological CV; maximize macro-F1 minus 0.25 x multiclass Brier; final holdout untouched until selected config",
    }


def tune_range(X: pd.DataFrame, y: np.ndarray, train_end: int) -> dict[str, Any]:
    X_train, X_hold = X.iloc[:train_end], X.iloc[train_end:]
    y_train, y_hold = y[:train_end], y[train_end:]
    candidates = [
        ("rf_depth4_leaf10", lambda: RandomForestRegressor(n_estimators=180, max_depth=4, min_samples_leaf=10, max_features=0.8, random_state=42, n_jobs=2)),
        ("rf_depth6_leaf8", lambda: RandomForestRegressor(n_estimators=220, max_depth=6, min_samples_leaf=8, max_features=0.8, random_state=42, n_jobs=2)),
        ("rf_depth8_leaf12", lambda: RandomForestRegressor(n_estimators=220, max_depth=8, min_samples_leaf=12, max_features=0.75, random_state=42, n_jobs=2)),
        ("extra_trees_depth5_leaf10", lambda: ExtraTreesRegressor(n_estimators=180, max_depth=5, min_samples_leaf=10, max_features=0.8, random_state=42, n_jobs=2)),
        ("extra_trees_depth8_leaf12", lambda: ExtraTreesRegressor(n_estimators=180, max_depth=8, min_samples_leaf=12, max_features=0.75, random_state=42, n_jobs=2)),
    ]
    splitter = TimeSeriesSplit(n_splits=3)
    baseline_fold_mae = []
    scores = []
    for tr, va in splitter.split(X_train):
        baseline = DummyRegressor(strategy="median").fit(X_train.iloc[tr], y_train[tr])
        baseline_fold_mae.append(_range_metrics(y_train[va], _range_prediction(baseline, X_train.iloc[va]))["combined_mae"])
    baseline_cv = float(np.mean(baseline_fold_mae))
    for name, factory in candidates:
        fold_mae = []
        for tr, va in splitter.split(X_train):
            model = factory()
            model.fit(X_train.iloc[tr], y_train[tr])
            fold_mae.append(_range_metrics(y_train[va], _range_prediction(model, X_train.iloc[va]))["combined_mae"])
        scores.append({"name": name, "cv_combined_mae": float(np.mean(fold_mae)), "folds": len(fold_mae)})
    selected = min(scores, key=lambda item: item["cv_combined_mae"])
    model = dict((name, factory) for name, factory in candidates)[selected["name"]]()
    model.fit(X_train, y_train)
    hold_pred = _range_prediction(model, X_hold)
    baseline = DummyRegressor(strategy="median").fit(X_train, y_train)
    baseline_pred = _range_prediction(baseline, X_hold)
    hold_metrics = _range_metrics(y_hold, hold_pred)
    baseline_metrics = _range_metrics(y_hold, baseline_pred)
    return {
        "selected_config": selected["name"],
        "selected_cv_combined_mae": selected["cv_combined_mae"],
        "baseline_cv_combined_mae": baseline_cv,
        "candidates": scores,
        "holdout": hold_metrics,
        "baseline_holdout": baseline_metrics,
        "beats_baseline": hold_metrics["combined_mae"] < baseline_metrics["combined_mae"],
        "postprocessing": "enforce predicted next-session low return <= close return <= high return",
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
    beats = (
        hold_metrics["pooled_mae"] < base_metrics["pooled_mae"]
        and hold_metrics["date_balanced_mae"] < base_metrics["date_balanced_mae"]
    )
    return {
        "selected_config": selected["config"],
        "selected_cv_pooled_mae": selected["cv_pooled_mae"],
        "selected_cv_date_balanced_mae": selected["cv_date_balanced_mae"],
        "baseline_cv_pooled_mae": float(np.mean(baseline_fold_pool)),
        "baseline_cv_date_balanced_mae": float(np.mean(baseline_fold_date)),
        "candidate_results": candidate_rows,
        "holdout": hold_metrics,
        "baseline_holdout": base_metrics,
        "beats_baseline": beats,
        "holdout_start": cutoff_date,
        "holdout_end": max(hold_dates),
        "split_policy": "chronological split and three expanding-window folds by trading date; no random contract-row split",
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
        "direction": tune_direction(X_daily, targets["direction"], split),
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
        "tuning_method": "chronological expanding-window validation; hyperparameters selected only on training portion; final 20 percent holdout evaluated once after selection",
        "seller_validation_method": "grouped by trading date, not random contract rows; reports both pooled and equal-weight per-date MAE",
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
