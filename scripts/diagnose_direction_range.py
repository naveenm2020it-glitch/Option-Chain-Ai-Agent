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
    """Add scale-normalized regime, momentum and volatility interactions."""
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
                X[five] - X[month]
            ) / (np.abs(X[month]) + 1e-6)
    vol = X.get("realized_volatility_22_sessions")
    if vol is not None:
        safe_vol = vol.clip(lower=0.001)
        for momentum in ("spot_return_5_sessions", "spot_return_22_sessions"):
            if momentum in X.columns:
                out[f"vol_adjusted__{momentum}"] = X[momentum] / safe_vol
        for distance in (
            "high_distance_5_sessions", "low_distance_5_sessions",
            "high_distance_22_sessions", "low_distance_22_sessions",
        ):
            if distance in X.columns:
                out[f"vol_adjusted__{distance}"] = X[distance] / safe_vol
    if "std5__spot_return_1d" in X.columns and "std22__spot_return_1d" in X.columns:
        out["short_long_volatility_ratio"] = (
            X["std5__spot_return_1d"] / (X["std22__spot_return_1d"] + 1e-6)
        )
    return out.replace([np.inf, -np.inf], np.nan)


def _adaptive_labels(returns: np.ndarray, volatility: np.ndarray, multiplier: float):
    # Volatility is calculated only from completed sessions at each forecast origin.
    threshold = np.clip(volatility * multiplier, 0.001, 0.006)
    labels = np.where(returns > threshold, 2, np.where(returns < -threshold, 0, 1))
    return labels.astype(int), threshold


def _fit_probability_calibration(raw, actual, prior):
    logp = np.log(np.clip(raw, 1e-8, 1.0))
    best = None
    for temperature in (0.65, 0.80, 1.0, 1.20, 1.50, 1.80):
        scaled = np.exp(logp / temperature)
        scaled /= scaled.sum(axis=1, keepdims=True)
        for blend in (0.0, 0.10, 0.20, 0.30, 0.40):
            probs = (1.0 - blend) * scaled + blend * prior
            metrics = _direction_metrics(actual, probs)
            score = metrics["multiclass_brier"] - 0.03 * metrics["macro_f1"]
            candidate = (score, temperature, blend)
            if best is None or candidate < best:
                best = candidate
    return best[1], best[2]


def _apply_probability_calibration(raw, prior, temperature, blend):
    scaled = np.exp(np.log(np.clip(raw, 1e-8, 1.0)) / temperature)
    scaled /= scaled.sum(axis=1, keepdims=True)
    return (1.0 - blend) * scaled + blend * prior


def diagnose_direction(X, close_returns, split):
    volatility = np.maximum(X["realized_volatility_22_sessions"].to_numpy(), 0.001)
    variants = {"base": X, "relative_regime_features": _enhanced_features(X)}
    candidates = []
    for variant_name, features in variants.items():
        for multiplier in (0.50, 0.75, 1.00, 1.25):
            labels, thresholds = _adaptive_labels(close_returns, volatility, multiplier)
            fold_metrics, fold_baselines = [], []
            for tr, va in TimeSeriesSplit(n_splits=3).split(features.iloc[:split]):
                if len(tr) < 80 or len(va) < 15:
                    continue
                fit_end = int(len(tr) * 0.80)
                fit_idx, cal_idx = tr[:fit_end], tr[fit_end:]
                if len(cal_idx) < 15 or set(np.unique(labels[fit_idx])) != {0, 1, 2}:
                    continue
                model = _classifier().fit(features.iloc[fit_idx], labels[fit_idx])
                prior = np.bincount(labels[fit_idx], minlength=3).astype(float) / len(fit_idx)
                cal_raw = _aligned(model, features.iloc[cal_idx])
                temperature, blend = _fit_probability_calibration(
                    cal_raw, labels[cal_idx], prior
                )
                valid_raw = _aligned(model, features.iloc[va])
                valid_probs = _apply_probability_calibration(
                    valid_raw, prior, temperature, blend
                )
                fold_metrics.append(_direction_metrics(labels[va], valid_probs))
                baseline = DummyClassifier(strategy="prior").fit(
                    features.iloc[fit_idx], labels[fit_idx]
                )
                fold_baselines.append(_direction_metrics(
                    labels[va], _aligned(baseline, features.iloc[va])
                ))
            if not fold_metrics:
                continue
            keys = ("macro_f1", "multiclass_brier", "accuracy")
            cv = {k: float(np.mean([m[k] for m in fold_metrics])) for k in keys}
            base_cv = {k: float(np.mean([m[k] for m in fold_baselines])) for k in keys}
            candidates.append({
                "feature_variant": variant_name,
                "volatility_threshold_multiplier": multiplier,
                "cv": cv,
                "prior_baseline_cv": base_cv,
                "cv_score": cv["macro_f1"] - 0.25 * cv["multiclass_brier"],
                "cv_gate": (
                    cv["macro_f1"] > base_cv["macro_f1"]
                    and cv["multiclass_brier"] < base_cv["multiclass_brier"]
                    and cv["accuracy"] >= base_cv["accuracy"] - 0.02
                ),
            })
    if not candidates:
        raise RuntimeError("No calibrated direction candidate produced valid chronological folds")
    feasible = [item for item in candidates if item["cv_gate"]]
    pool = feasible if feasible else candidates
    selected = max(pool, key=lambda item: item["cv_score"])
    labels, thresholds = _adaptive_labels(
        close_returns, volatility, selected["volatility_threshold_multiplier"]
    )
    features = variants[selected["feature_variant"]]
    fit_end = int(split * 0.80)
    fit_idx, cal_idx = np.arange(fit_end), np.arange(fit_end, split)
    model = _classifier().fit(features.iloc[fit_idx], labels[fit_idx])
    prior = np.bincount(labels[fit_idx], minlength=3).astype(float) / len(fit_idx)
    temperature, blend = _fit_probability_calibration(
        _aligned(model, features.iloc[cal_idx]), labels[cal_idx], prior
    )
    hold_probs = _apply_probability_calibration(
        _aligned(model, features.iloc[split:]), prior, temperature, blend
    )
    model_hold = _direction_metrics(labels[split:], hold_probs)
    baseline = DummyClassifier(strategy="prior").fit(features.iloc[fit_idx], labels[fit_idx])
    baseline_hold = _direction_metrics(
        labels[split:], _aligned(baseline, features.iloc[split:])
    )
    return {
        "selected_feature_variant_from_training_cv": selected["feature_variant"],
        "selected_volatility_multiplier_from_training_cv": selected["volatility_threshold_multiplier"],
        "selected_training_cv": selected["cv"],
        "selected_prior_baseline_cv": selected["prior_baseline_cv"],
        "selected_calibration": {"temperature": temperature, "prior_blend": blend},
        "candidate_count": len(candidates),
        "candidate_cv_summary": candidates,
        "exploratory_holdout": model_hold,
        "exploratory_prior_baseline_holdout": baseline_hold,
        "exploratory_holdout_beats_baseline": (
            model_hold["macro_f1"] > baseline_hold["macro_f1"]
            and model_hold["multiclass_brier"] < baseline_hold["multiclass_brier"]
            and model_hold["accuracy"] >= baseline_hold["accuracy"] - 0.02
        ),
        "calibration_policy": "Temperature scaling and prior blending are fitted only on the chronological calibration tail inside each training fold.",
        "label_policy": "Per-origin neutral band uses clipped trailing-22-session realized volatility; volatility uses completed sessions only.",
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
