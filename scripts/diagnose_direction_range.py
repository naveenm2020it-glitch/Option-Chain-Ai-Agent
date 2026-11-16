from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
from sklearn.dummy import DummyClassifier
from sklearn.model_selection import TimeSeriesSplit
from lightgbm import LGBMClassifier

from train_nifty_four_models import (
    EXPECTED_SESSIONS, _check_artifact_gates, _build_daily_targets,
    _direction_metrics, _feature_matrix, build_rolling_feature_rows,
)


def _aligned(model, X):
    raw = model.predict_proba(X)
    out = np.zeros((len(X), 3), dtype=float)
    for j, label in enumerate(model.classes_):
        out[:, int(label)] = raw[:, j]
    return out


def _classifier():
    return LGBMClassifier(
        objective="multiclass", num_class=3, class_weight="balanced",
        n_estimators=140, learning_rate=0.025, num_leaves=5, max_depth=2,
        min_child_samples=35, reg_lambda=20.0, reg_alpha=3.0,
        colsample_bytree=0.8, subsample=0.85, subsample_freq=1,
        verbosity=-1, random_state=42, n_jobs=2,
    )


def _threshold_labels(returns, threshold):
    return np.where(returns > threshold, 2, np.where(returns < -threshold, 0, 1)).astype(int)


def diagnose_direction(X, close_returns, split):
    X_train, X_hold = X.iloc[:split], X.iloc[split:]
    returns_train, returns_hold = close_returns[:split], close_returns[split:]
    candidates = []
    for threshold in (0.001, 0.0015, 0.002, 0.0025, 0.003, 0.004):
        y_train = _threshold_labels(returns_train, threshold)
        y_hold = _threshold_labels(returns_hold, threshold)
        if set(np.unique(y_train)) != {0, 1, 2}:
            continue
        fold_model, fold_base = [], []
        for tr, va in TimeSeriesSplit(n_splits=3).split(X_train):
            y_tr, y_va = y_train[tr], y_train[va]
            if set(np.unique(y_tr)) != {0, 1, 2}:
                continue
            model = _classifier().fit(X_train.iloc[tr], y_tr)
            prior = DummyClassifier(strategy="prior").fit(X_train.iloc[tr], y_tr)
            fold_model.append(_direction_metrics(y_va, _aligned(model, X_train.iloc[va])))
            fold_base.append(_direction_metrics(y_va, _aligned(prior, X_train.iloc[va])))
        if not fold_model:
            continue
        keys = ("macro_f1", "multiclass_brier", "accuracy")
        cv = {k: float(np.mean([m[k] for m in fold_model])) for k in keys}
        baseline_cv = {k: float(np.mean([m[k] for m in fold_base])) for k in keys}
        candidates.append({
            "threshold": threshold,
            "train_class_counts": {str(i): int(np.sum(y_train == i)) for i in range(3)},
            "holdout_class_counts": {str(i): int(np.sum(y_hold == i)) for i in range(3)},
            "cv": cv, "baseline_cv": baseline_cv,
            "selection_score": cv["macro_f1"] - 0.25 * cv["multiclass_brier"],
        })
    if not candidates:
        raise RuntimeError("No direction threshold produced valid three-class chronological folds")
    selected = max(candidates, key=lambda x: x["selection_score"])
    threshold = selected["threshold"]
    y_train = _threshold_labels(returns_train, threshold)
    y_hold = _threshold_labels(returns_hold, threshold)
    model = _classifier().fit(X_train, y_train)
    baseline = DummyClassifier(strategy="prior").fit(X_train, y_train)
    holdout = _direction_metrics(y_hold, _aligned(model, X_hold))
    baseline_holdout = _direction_metrics(y_hold, _aligned(baseline, X_hold))
    return {
        "threshold_candidates": candidates,
        "selected_threshold_from_training_cv": threshold,
        "selected_model_holdout": holdout,
        "prior_baseline_holdout": baseline_holdout,
        "holdout_beats_baseline": (
            holdout["macro_f1"] > baseline_holdout["macro_f1"]
            and holdout["multiclass_brier"] < baseline_holdout["multiclass_brier"]
        ),
        "policy": "threshold chosen only by expanding-window CV on first 80%; locked final 20% evaluated once",
    }


def diagnose_range_baselines(range_targets, split):
    # Each origin can use only outcomes from earlier origins; no target leakage.
    predictions = []
    for i in range(len(range_targets)):
        history = range_targets[max(0, i - 5):i]
        if len(history) < 3:
            predictions.append(np.median(range_targets[:max(1, i)], axis=0))
        else:
            predictions.append(np.median(history, axis=0))
    pred = np.asarray(predictions)
    actual = range_targets[split:]
    rolling = pred[split:]
    global_median = np.median(range_targets[:split], axis=0)
    median_pred = np.repeat(global_median[None, :], len(actual), axis=0)
    def metrics(y, p):
        return {
            "combined_mae": float(np.mean(np.abs(y - p))),
            "low_mae": float(np.mean(np.abs(y[:, 0] - p[:, 0]))),
            "high_mae": float(np.mean(np.abs(y[:, 1] - p[:, 1]))),
            "close_mae": float(np.mean(np.abs(y[:, 2] - p[:, 2]))),
            "close_inside_predicted_range": float(np.mean((y[:, 2] >= p[:, 0]) & (y[:, 2] <= p[:, 1]))),
        }
    # Enforce physical ordering for both baseline ranges.
    for p in (rolling, median_pred):
        p[:, 0] = np.minimum(p[:, 0], p[:, 2])
        p[:, 1] = np.maximum(p[:, 1], p[:, 2])
    return {
        "previous_five_sessions_median": metrics(actual, rolling),
        "training_global_median": metrics(actual, median_pred),
        "rolling_baseline_policy": "At each origin, use only the preceding up-to-five realized session outcomes; never the current target.",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--checkpoint", type=Path, default=Path("data/historical_audit/checkpoint.json"))
    parser.add_argument("--audit-report", type=Path, default=Path("data/historical_audit/coverage.json"))
    parser.add_argument("--repair-report", type=Path, default=Path("data/context_repair/context_repair_report.json"))
    parser.add_argument("--output", type=Path, default=Path("models/tuning_artifacts/direction_range_diagnostics.json"))
    args = parser.parse_args()
    checkpoint, audit, repair = _check_artifact_gates(args.checkpoint, args.audit_report, args.repair_report)
    rows = checkpoint["features"]
    if len(rows) != EXPECTED_SESSIONS:
        raise RuntimeError("Diagnostics require the verified 494-session archive")
    rolling = build_rolling_feature_rows(rows)
    X, names = _feature_matrix(rolling)
    targets = _build_daily_targets(rolling, rows)
    split = int(len(X) * 0.8)
    close_returns = targets["close_return"]
    range_targets = np.column_stack((targets["low_return"], targets["high_return"], targets["close_return"]))
    report = {
        "status": "diagnostics_complete",
        "archive_sessions": len(rows),
        "daily_samples": len(X),
        "feature_count": len(names),
        "locked_holdout_samples": len(X) - split,
        "direction": diagnose_direction(X, close_returns, split),
        "range_baselines": diagnose_range_baselines(range_targets, split),
        "b2_reads": 0,
        "b2_writes": 0,
        "deployment_ready": False,
        "note": "Research diagnostics only. No production model or Render service is modified.",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
