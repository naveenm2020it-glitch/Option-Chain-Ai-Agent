from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from lightgbm import LGBMClassifier
from sklearn.dummy import DummyClassifier, DummyRegressor
from sklearn.ensemble import ExtraTreesRegressor, RandomForestRegressor
from sklearn.model_selection import TimeSeriesSplit

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


def _enhanced_features(X: pd.DataFrame) -> pd.DataFrame:
    """Add scale-normalized 5-session versus 22-session regime shifts."""
    out = X.copy()
    sources = (
        "future_return", "future_oi_change_pct", "pcr_oi", "pcr_change_oi",
        "pcr_volume", "ce_writer_build_share", "pe_writer_build_share",
        "participant_oi_fii_future_index_net",
        "participant_oi_client_future_index_net",
        "participant_oi_fii_option_index_call_net",
        "participant_oi_fii_option_index_put_net",
        "participant_volume_fii_option_index_call_net",
        "participant_volume_fii_option_index_put_net",
        "spot_return_1d", "spot_open_gap_pct", "spot_close_location",
    )
    for source in sources:
        five, month = f"mean5__{source}", f"mean22__{source}"
        if five in X.columns and month in X.columns:
            out[f"relative_regime_shift__{source}"] = (
                X[five"] - X[month]
            ) / (np.abs(X[month]) + 1e-6)
    return out.replace([np.inf, -np.inf], np.nan)


def _adaptive_labels(returns: np.ndarray, volatility: np.ndarray, multiplier: float):
    # Volatility is calculated only from completed sessions at each forecast origin.
    threshold = np.clip(volatility * multiplier, 0.001, 0.006)
    labels = np.where(returns > threshold, 2, np.where(returns < -threshold, 0, 1))
    return labels.astype(int), threshold


def diagnose_direction(X, close_returns, split):
    volatility = np.maximum(X["realized_volatility_22_sessions"].to_numpy(), 0.001)
    variants = {"base": X, "relative_regime_features": _enhanced_features(X)}
    candidates = []
    for variant_name, features in variants.items():
        for multiplier in (0.50, 0.75, 1.00, 1.25):
            labels, thresholds = _adaptive_labels(close_returns, volatility, multiplier)
            train_labels = labels[:split]
            if set(np.unique(train_labels)) != {0, 1, 2}:
                continue
            fold_metrics, fold_baselines = [], []
            for tr, va in TimeSeriesSplit(n_splits=3).split(features.iloc[:split]):
                if set(np.unique(train_labels[tr])) != {0, 1, 2}:
                    continue
                model = _classifier().fit(features.iloc[tr], train_labels[tr])
                baseline = DummyClassifier(strategy="prior").fit(features.iloc[tr], train_labels[tr])
                fold_metrics.append(_direction_metrics(
                    train_labels[va], _aligned(model, features.iloc[va])
                ))
                fold_baselines.append(_direction_metrics(
                    train_labels[va], _aligned(baseline, features.iloc[va])
                ))
            if not fold_metrics:
                continue
            metric_keys = ("macro_f1", "multiclass_brier", "accuracy")
            cv = {k: float(np.mean([m[k] for m in fold_metrics])) for k in metric_keys}
            base_cv = {k: float(np.mean([m[k] for m in fold_baselines])) for k in metric_keys}
            candidates.append({
                "feature_variant": variant_name,
                "volatility_threshold_multiplier": multiplier,
                "cv": cv,
                "prior_baseline_cv": base_cv,
                "cv_score": cv["macro_f1"] - 0.25 * cv["multiclass_brier"],
                "mean_training_threshold": float(np.mean(thresholds[:split])),
                "training_class_counts": {
                    str(k): int(np.sum(train_labels == k)) for k in range(3)
                },
            })
    if not candidates:
        raise RuntimeError("No adaptive direction candidate produced valid chronological folds")
    selected = max(candidates, key=lambda item: item["cv_score"])
    selected_features = variants[selected["feature_variant"]]
    labels, thresholds = _adaptive_labels(
        close_returns, volatility, selected["volatility_threshold_multiplier"]
    )
    model = _classifier().fit(selected_features.iloc[:split], labels[:split])
    baseline = DummyClassifier(strategy="prior").fit(selected_features.iloc[:split], labels[:split])
    model_hold = _direction_metrics(labels[split:], _aligned(model, selected_features.iloc[split:]))
    baseline_hold = _direction_metrics(labels[split:], _aligned(baseline, selected_features.iloc[split:]))
    return {
        "selected_feature_variant_from_training_cv": selected["feature_variant"],
        "selected_volatility_multiplier_from_training_cv": selected["volatility_threshold_multiplier"],
        "selected_training_cv": selected["cv"],
        "selected_prior_baseline_cv": selected["prior_baseline_cv"],
        "candidate_count": len(candidates),
        "candidate_cv_summary": candidates,
        "exploratory_holdout": model_hold,
        "exploratory_prior_baseline_holdout": baseline_hold,
        "exploratory_holdout_beats_baseline": (
            model_hold["macro_f1"] > baseline_hold["macro_f1"]
            and model_hold["multiclass_brier"] < baseline_hold["multiclass_brier"]
        ),
        "label_policy": "Per-origin neutral band = clipped trailing-22-session realized volatility times a multiplier; volatility uses completed sessions only.",
    }


def _range_metrics(actual, prediction):
    low, high, close = prediction[:, 0], prediction[:, 1], prediction[:, 2]
    return {
        "combined_mae": float(np.mean(np.abs(actual - prediction))),
        "low_mae": float(np.mean(np.abs(actual[:, 0] - low))),
        "high_mae": float(np.mean(np.abs(actual[:, 1] - high))),
        "close_mae": float(np.mean(np.abs(actual[:, 2] - close))),
        "close_inside_predicted_range": float(np.mean(
            (actual[:, 2] >= low) & (actual[:, 2] <= high)
        )),
    }


def _ordered_range(prediction):
    out = np.asarray(prediction, dtype=float).copy()
    out[:, 0] = np.minimum(out[:, 0], out[:, 2])
    out[:, 1] = np.maximum(out[:, 1], out[:, 2])
    return out


def diagnose_range(X, targets, split):
    # Scale each day's low/high/close return by volatility known at its origin.
    scale = np.maximum(X["realized_volatility_22_sessions"].to_numpy(), 0.001)
    normalized = targets / scale[:, None]
    candidates = [
        ("extra_trees_normalized", lambda: ExtraTreesRegressor(
            n_estimators=220, max_depth=5, min_samples_leaf=10,
            max_features=0.8, random_state=42, n_jobs=2,
        )),
        ("random_forest_normalized", lambda: RandomForestRegressor(
            n_estimators=220, max_depth=5, min_samples_leaf=10,
            max_features=0.8, random_state=42, n_jobs=2,
        )),
    ]
    splitter = TimeSeriesSplit(n_splits=3)
    baseline_scores, model_scores = [], {name: [] for name, _ in candidates}
    for tr, va in splitter.split(X.iloc[:split]):
        baseline = DummyRegressor(strategy="median").fit(X.iloc[tr], normalized[tr])
        baseline_pred = _ordered_range(baseline.predict(X.iloc[va]) * scale[va, None])
        baseline_scores.append(_range_metrics(targets[va], baseline_pred)["combined_mae"])
        for name, factory in candidates:
            model = factory().fit(X.iloc[tr], normalized[tr])
            prediction = _ordered_range(model.predict(X.iloc[va]) * scale[va, None])
            model_scores[name].append(_range_metrics(targets[va], prediction)["combined_mae"])
    baseline_cv = float(np.mean(baseline_scores))
    candidate_cv = {
        name: float(np.mean(scores)) for name, scores in model_scores.items()
    }
    selected_name = min(candidate_cv, key=candidate_cv.get)
    selected_factory = dict(candidates)[selected_name]
    model = selected_factory().fit(X.iloc[:split], normalized[:split])
    prediction = _ordered_range(model.predict(X.iloc[split:]) * scale[split:, None])
    baseline = DummyRegressor(strategy="median").fit(X.iloc[:split], normalized[:split])
    baseline_prediction = _ordered_range(
        baseline.predict(X.iloc[split:]) * scale[split:, None]
    )
    model_hold = _range_metrics(targets[split:], prediction)
    baseline_hold = _range_metrics(targets[split:], baseline_prediction)
    return {
        "target_policy": "Next-session low/high/close returns normalized by trailing-22-session realized volatility known at forecast origin.",
        "selected_model_from_training_cv": selected_name,
        "candidate_cv_combined_mae": candidate_cv,
        "normalized_median_baseline_cv_combined_mae": baseline_cv,
        "exploratory_holdout": model_hold,
        "exploratory_baseline_holdout": baseline_hold,
        "exploratory_holdout_beats_baseline": (
            model_hold["combined_mae"] < baseline_hold["combined_mae"]
        ),
        "prediction_ordering": "low <= close <= high enforced after inverse volatility scaling",
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
    range_targets = np.column_stack((
        targets["low_return"], targets["high_return"], targets["close_return"]
    ))
    report = {
        "status": "diagnostics_complete",
        "archive_sessions": len(rows),
        "daily_samples": len(X),
        "feature_count": len(names),
        "locked_holdout_samples": len(X) - split,
        "direction": diagnose_direction(X, targets["close_return"], split),
        "range": diagnose_range(X, range_targets, split),
        "holdout_status": "exploratory_only_existing_holdout_has_already_been_inspected; do_not_use_for_promotion",
        "validation_policy": "Candidate selection uses training-only expanding-window CV. The historical final 20% is reported for diagnosis only because earlier iterations already inspected it; a fresh future-period shadow test is required for promotion.",
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
