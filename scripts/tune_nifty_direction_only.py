from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import joblib
import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.calibration import CalibratedClassifierCV
from sklearn.dummy import DummyClassifier
from sklearn.ensemble import HistGradientBoostingClassifier, RandomForestClassifier
from sklearn.metrics import balanced_accuracy_score, f1_score, log_loss
from sklearn.model_selection import TimeSeriesSplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from scripts.train_nifty_four_models import (
    EXPECTED_SESSIONS,
    DIRECTION_THRESHOLD,
    _check_artifact_gates,
    _feature_matrix,
    build_rolling_feature_rows,
)


LABEL_NAMES = {0: "bearish", 1: "neutral", 2: "bullish"}
THRESHOLDS = (0.0015, 0.0020, 0.0025, 0.0030, 0.0040)
RANDOM_STATE = 42


def direction_labels(next_returns: np.ndarray, threshold: float) -> np.ndarray:
    values = np.asarray(next_returns, dtype=float)
    if values.ndim != 1 or not np.isfinite(values).all():
        raise ValueError("Direction returns must be a finite one-dimensional array")
    if not 0 < threshold < 0.05:
        raise ValueError("Direction threshold must be between 0 and 5 percent")
    return np.where(values > threshold, 2, np.where(values < -threshold, 0, 1)).astype(int)


def probability_matrix(model: Any, features: pd.DataFrame) -> np.ndarray:
    raw = np.asarray(model.predict_proba(features), dtype=float)
    classes = np.asarray(model.classes_, dtype=int)
    aligned = np.zeros((len(features), 3), dtype=float)
    for index, label in enumerate(classes):
        if label not in (0, 1, 2):
            raise ValueError("Unexpected direction class in fitted model")
        aligned[:, label] = raw[:, index]
    totals = aligned.sum(axis=1, keepdims=True)
    if not np.isfinite(aligned).all() or np.any(totals <= 0):
        raise ValueError("Direction model returned invalid probabilities")
    return aligned / totals


def expected_calibration_error(y_true: np.ndarray, probabilities: np.ndarray, bins: int = 10) -> float:
    confidence = np.max(probabilities, axis=1)
    prediction = np.argmax(probabilities, axis=1)
    correct = (prediction == y_true).astype(float)
    edges = np.linspace(0.0, 1.0, bins + 1)
    result = 0.0
    for index in range(bins):
        mask = (confidence >= edges[index]) & (
            confidence <= edges[index + 1] if index == bins - 1 else confidence < edges[index + 1]
        )
        if np.any(mask):
            result += float(np.mean(mask)) * abs(float(np.mean(correct[mask])) - float(np.mean(confidence[mask])))
    return float(result)


def direction_metrics(y_true: np.ndarray, probabilities: np.ndarray) -> dict[str, Any]:
    if len(y_true) != len(probabilities) or len(y_true) == 0:
        raise ValueError("Direction metrics require non-empty aligned labels and probabilities")
    if probabilities.shape != (len(y_true), 3):
        raise ValueError("Direction probabilities must have exactly three columns")
    prediction = np.argmax(probabilities, axis=1)
    recalls = {}
    for label, name in LABEL_NAMES.items():
        mask = y_true == label
        recalls[name] = float(np.mean(prediction[mask] == label)) if np.any(mask) else None
    return {
        "macro_f1": float(f1_score(y_true, prediction, labels=[0, 1, 2], average="macro", zero_division=0)),
        "balanced_accuracy": float(balanced_accuracy_score(y_true, prediction)),
        "accuracy": float(np.mean(prediction == y_true)),
        "multiclass_brier": float(np.mean(np.sum((probabilities - np.eye(3)[y_true]) ** 2, axis=1))),
        "log_loss": float(log_loss(y_true, probabilities, labels=[0, 1, 2])),
        "expected_calibration_error": expected_calibration_error(y_true, probabilities),
        "class_recall": recalls,
    }


def candidate_factories() -> dict[str, Any]:
    return {
        "lightgbm_balanced_compact": lambda: LGBMClassifier(
            objective="multiclass", num_class=3, class_weight="balanced",
            n_estimators=160, learning_rate=0.025, num_leaves=7, max_depth=3,
            min_child_samples=20, reg_lambda=12.0, reg_alpha=2.0,
            colsample_bytree=0.75, subsample=0.8, subsample_freq=1,
            verbosity=-1, random_state=RANDOM_STATE, n_jobs=2,
        ),
        "lightgbm_unweighted_compact": lambda: LGBMClassifier(
            objective="multiclass", num_class=3, class_weight=None,
            n_estimators=160, learning_rate=0.025, num_leaves=7, max_depth=3,
            min_child_samples=20, reg_lambda=12.0, reg_alpha=2.0,
            colsample_bytree=0.75, subsample=0.8, subsample_freq=1,
            verbosity=-1, random_state=RANDOM_STATE, n_jobs=2,
        ),
        "hist_gradient_boosting": lambda: HistGradientBoostingClassifier(
            max_iter=120, learning_rate=0.05, max_leaf_nodes=7,
            l2_regularization=8.0, min_samples_leaf=18,
            early_stopping=False, random_state=RANDOM_STATE,
        ),
        "random_forest_balanced": lambda: RandomForestClassifier(
            n_estimators=180, max_depth=5, min_samples_leaf=8,
            max_features=0.75, class_weight="balanced_subsample",
            random_state=RANDOM_STATE, n_jobs=2,
        ),
    }


def _fit_candidate(factory: Any, name: str, X: pd.DataFrame, y: np.ndarray, calibrated: bool):
    if calibrated:
        model = CalibratedClassifierCV(
            estimator=factory(), method="sigmoid",
            cv=TimeSeriesSplit(n_splits=3), ensemble=True,
        )
    else:
        model = factory()
    model.fit(X, y)
    return model


def compare_direction_candidates(X: pd.DataFrame, y: np.ndarray, n_splits: int = 4) -> dict[str, Any]:
    if len(X) != len(y) or len(X) < 250:
        raise ValueError("Direction benchmark requires at least 250 aligned samples")
    counts = np.bincount(y, minlength=3)
    if np.min(counts) < 30:
        raise ValueError(f"Direction class count below 30: {counts.tolist()}")

    splitter = TimeSeriesSplit(n_splits=n_splits)
    factories = candidate_factories()
    results: dict[str, Any] = {}
    baseline_folds = []
    folds = list(splitter.split(X))

    for fold_number, (train_index, valid_index) in enumerate(folds, start=1):
        y_train = y[train_index]
        if set(np.unique(y_train)) != {0, 1, 2}:
            raise ValueError(f"Walk-forward fold {fold_number} training window lacks a class")
        baseline = DummyClassifier(strategy="prior")
        baseline.fit(X.iloc[train_index], y_train)
        baseline_probabilities = probability_matrix(baseline, X.iloc[valid_index])
        baseline_folds.append({"fold": fold_number, **direction_metrics(y[valid_index], baseline_probabilities)})

    results["prior_baseline"] = {
        "folds": baseline_folds,
        "mean": {
            key: float(np.mean([fold[key] for fold in baseline_folds]))
            for key in ("macro_f1", "balanced_accuracy", "accuracy", "multiclass_brier", "log_loss", "expected_calibration_error")
        },
    }

    for name, factory in factories.items():
        for calibrated in (False, True):
            variant = name + ("_sigmoid_calibrated" if calibrated else "_raw")
            fold_rows = []
            for fold_number, (train_index, valid_index) in enumerate(folds, start=1):
                try:
                    model = _fit_candidate(factory, name, X.iloc[train_index], y[train_index], calibrated)
                    probabilities = probability_matrix(model, X.iloc[valid_index])
                    fold_rows.append({"fold": fold_number, **direction_metrics(y[valid_index], probabilities)})
                except Exception as exc:
                    fold_rows = []
                    results[variant] = {"status": "candidate_failed", "error": f"{type(exc).__name__}: {str(exc)[:400]}"}
                    break
            if fold_rows:
                metric_keys = ("macro_f1", "balanced_accuracy", "accuracy", "multiclass_brier", "log_loss", "expected_calibration_error")
                results[variant] = {
                    "status": "evaluated",
                    "folds": fold_rows,
                    "mean": {key: float(np.mean([row[key] for row in fold_rows])) for key in metric_keys},
                    "std": {key: float(np.std([row[key] for row in fold_rows])) for key in metric_keys},
                }

    evaluated = {
        name: value for name, value in results.items()
        if name != "prior_baseline" and value.get("status") == "evaluated"
    }
    if not evaluated:
        raise RuntimeError("No Direction candidate completed every chronological fold")
    selected = min(
        evaluated,
        key=lambda name: (
            evaluated[name]["mean"]["multiclass_brier"],
            -evaluated[name]["mean"]["macro_f1"],
            evaluated[name]["mean"]["log_loss"],
        ),
    )
    baseline = results["prior_baseline"]["mean"]
    selected_mean = evaluated[selected]["mean"]
    beats_baseline = (
        selected_mean["multiclass_brier"] < baseline["multiclass_brier"]
        and selected_mean["macro_f1"] > baseline["macro_f1"]
    )
    return {
        "selected_candidate": selected,
        "candidate_results": results,
        "selected_cv_beats_prior_baseline": bool(beats_baseline),
        "selection_rule": "lowest mean chronological CV multiclass Brier; tie-break higher macro-F1 then lower log-loss",
        "fold_count": n_splits,
    }


def run_direction_research(
    checkpoint_path: Path, audit_path: Path, repair_path: Path,
    output_path: Path, bundle_path: Path,
) -> dict[str, Any]:
    checkpoint, audit, repair = _check_artifact_gates(checkpoint_path, audit_path, repair_path)
    feature_rows = checkpoint["features"]
    if len(feature_rows) != EXPECTED_SESSIONS:
        raise RuntimeError("Direction tuning requires the verified 494-session archive")
    rolling = build_rolling_feature_rows(feature_rows)
    X, feature_names = _feature_matrix(rolling)
    ordered = sorted(rolling, key=lambda row: row["target_date"])
    next_returns = []
    for sample in ordered:
        origin = feature_rows[sample["origin_date"]]["features"]
        target = feature_rows[sample["target_date"]]["features"]
        previous_close = float(origin["spot_close"])
        next_close = float(target["spot_close"])
        if previous_close <= 0 or next_close <= 0:
            raise ValueError("Direction target contains a non-positive index close")
        next_returns.append(next_close / previous_close - 1.0)
    next_returns = np.asarray(next_returns, dtype=float)
    split = int(len(X) * 0.80)
    if split < 250 or len(X) - split < 50:
        raise ValueError("Direction chronological train/diagnostic split is too small")

    threshold_reports = []
    selected_overall = None
    for threshold in THRESHOLDS:
        labels = direction_labels(next_returns, threshold)
        class_counts = np.bincount(labels, minlength=3)
        if np.min(class_counts) < 30:
            threshold_reports.append({
                "threshold": threshold, "status": "skipped_class_imbalance",
                "class_counts": {LABEL_NAMES[i]: int(class_counts[i]) for i in range(3)},
            })
            continue
        try:
            comparison = compare_direction_candidates(X.iloc[:split].reset_index(drop=True), labels[:split])
        except ValueError as exc:
            threshold_reports.append({
                "threshold": threshold, "status": "skipped_cv_constraints",
                "class_counts": {LABEL_NAMES[i]: int(class_counts[i]) for i in range(3)},
                "reason": str(exc),
            })
            continue
        candidate = comparison["selected_candidate"]
        score = comparison["candidate_results"][candidate]["mean"]
        threshold_reports.append({
            "threshold": threshold, "status": "evaluated",
            "class_counts": {LABEL_NAMES[i]: int(class_counts[i]) for i in range(3)},
            "comparison": comparison,
        })
        rank = (score["multiclass_brier"], -score["macro_f1"], score["log_loss"])
        if selected_overall is None or rank < selected_overall["rank"]:
            selected_overall = {
                "threshold": threshold, "candidate": candidate,
                "comparison": comparison, "rank": rank,
            }

    if selected_overall is None:
        raise RuntimeError("No Direction threshold passed minimum class-count and chronological CV constraints")

    threshold = selected_overall["threshold"]
    labels = direction_labels(next_returns, threshold)
    X_train, X_diagnostic = X.iloc[:split], X.iloc[split:]
    y_train, y_diagnostic = labels[:split], labels[split:]
    candidate_name = selected_overall["candidate"]
    calibrated = candidate_name.endswith("_sigmoid_calibrated")
    factory_name = candidate_name.removesuffix("_sigmoid_calibrated").removesuffix("_raw")
    factory = candidate_factories()[factory_name]
    diagnostic_model = _fit_candidate(factory, factory_name, X_train, y_train, calibrated)
    diagnostic_probabilities = probability_matrix(diagnostic_model, X_diagnostic)
    diagnostic_metrics = direction_metrics(y_diagnostic, diagnostic_probabilities)
    diagnostic_baseline = DummyClassifier(strategy="prior")
    diagnostic_baseline.fit(X_train, y_train)
    baseline_metrics = direction_metrics(
        y_diagnostic, probability_matrix(diagnostic_baseline, X_diagnostic)
    )

    final_model = _fit_candidate(factory, factory_name, X, labels, calibrated)
    bundle = {
        "model_version": "nifty-direction-research-v1",
        "model_group": "Direction",
        "model": final_model,
        "feature_names": feature_names,
        "direction_labels": LABEL_NAMES,
        "direction_threshold": threshold,
        "probability_columns": ["bearish", "neutral", "bullish"],
        "calibration": "sigmoid" if calibrated else "none",
        "window_sessions": {"recent": 5, "monthly": 22},
        "deployment_ready": False,
        "production_promotion_allowed": False,
    }
    bundle_path.parent.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, bundle_path, compress=3)
    model_hash = hashlib.sha256(bundle_path.read_bytes()).hexdigest()

    report = {
        "status": "direction_only_research_complete",
        "model_group": "Direction",
        "archive_sessions": len(feature_rows),
        "daily_samples": len(X),
        "feature_count": len(feature_names),
        "train_cv_samples": split,
        "historical_diagnostic_samples": len(X) - split,
        "holdout_policy": "The historical 20% segment is diagnostic only and is not an untouched independent test. Candidate and threshold selection use chronological CV inside the earlier 80%.",
        "threshold_search": threshold_reports,
        "selected_threshold": threshold,
        "selected_candidate": candidate_name,
        "selected_cv": selected_overall["comparison"]["candidate_results"][candidate_name],
        "selected_cv_beats_prior_baseline": selected_overall["comparison"]["selected_cv_beats_prior_baseline"],
        "historical_diagnostic": diagnostic_metrics,
        "historical_diagnostic_prior_baseline": baseline_metrics,
        "historical_diagnostic_beats_baseline": (
            diagnostic_metrics["macro_f1"] > baseline_metrics["macro_f1"]
            and diagnostic_metrics["multiclass_brier"] < baseline_metrics["multiclass_brier"]
        ),
        "class_counts": {LABEL_NAMES[i]: int(np.sum(labels == i)) for i in range(3)},
        "class_threshold_policy": "bearish if next close return < -threshold; bullish if > threshold; otherwise neutral",
        "archive_gates": {
            "audit_training_allowed": audit.get("model_training_allowed"),
            "repair_training_allowed": repair.get("training_allowed"),
            "date_alignment_pass": repair.get("date_alignment_pass"),
            "feature_validation_pass": repair.get("feature_validation", {}).get("pass"),
        },
        "b2_reads": 0,
        "b2_writes": 0,
        "candidate_bundle": str(bundle_path),
        "candidate_bundle_sha256": model_hash,
        "deployment_ready": False,
        "production_promotion_allowed": False,
        "other_models_tuned": False,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="NIFTY Direction-only chronological research benchmark")
    parser.add_argument("--checkpoint", type=Path, default=Path("data/historical_audit/checkpoint.json"))
    parser.add_argument("--audit-report", type=Path, default=Path("data/historical_audit/coverage.json"))
    parser.add_argument("--repair-report", type=Path, default=Path("data/context_repair/context_repair_report.json"))
    parser.add_argument("--output", type=Path, default=Path("models/tuning_artifacts/direction_only_report.json"))
    parser.add_argument("--bundle", type=Path, default=Path("models/artifacts/nifty_direction_candidate.joblib"))
    args = parser.parse_args()
    report = run_direction_research(
        args.checkpoint, args.audit_report, args.repair_report, args.output, args.bundle
    )
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
