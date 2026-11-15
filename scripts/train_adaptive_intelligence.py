from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.nse_ml_features import FEATURE_SCHEMA_VERSION


DIRECTION_LABELS = {0: "bearish", 1: "range", 2: "bullish"}
DIRECTION_THRESHOLD = 0.0025
LARGE_MOVE_THRESHOLD = 0.0075
MIN_SESSIONS = 300
MIN_CLASS_SAMPLES = 30
RANDOM_STATE = 42
TRAINING_PIPELINE_VERSION = 2


def _load_ml_dependencies() -> None:
    # Imported only after the historical integrity + feature gates pass. Daily
    # audit batches therefore do not install or load heavyweight ML libraries.
    global joblib, CatBoostClassifier, LGBMClassifier, LGBMRegressor
    global CalibratedClassifierCV, DummyClassifier, HistGradientBoostingRegressor
    global f1_score, mean_absolute_error, pinball_loss, TimeSeriesSplit
    global NearestNeighbors, StandardScaler, GaussianMixture
    import joblib
    from catboost import CatBoostClassifier
    from lightgbm import LGBMClassifier, LGBMRegressor
    from sklearn.calibration import CalibratedClassifierCV
    from sklearn.dummy import DummyClassifier
    from sklearn.ensemble import HistGradientBoostingRegressor
    from sklearn.metrics import f1_score, mean_absolute_error, mean_pinball_loss as pinball_loss
    from sklearn.model_selection import TimeSeriesSplit
    from sklearn.neighbors import NearestNeighbors
    from sklearn.preprocessing import StandardScaler
    from sklearn.mixture import GaussianMixture


def _canonical_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def check_training_gate(checkpoint_path: Path) -> tuple[dict, dict]:
    report_path = checkpoint_path.parent / "coverage.json"
    if not checkpoint_path.is_file() or not report_path.is_file():
        raise RuntimeError("Training blocked: historical audit checkpoint/report is missing")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    if report.get("status") != "archive_integrity_complete_calendar_reconciled":
        raise RuntimeError("Training blocked: 494-session archive integrity audit is not complete")
    if not report.get("model_training_allowed"):
        raise RuntimeError("Training blocked: every expected session must have verified ZIP integrity and parsed NIFTY features")
    expected = int(report.get("expected_session_count", 0))
    if expected != 494:
        raise RuntimeError(f"Training blocked: expected 494 NSE F&O sessions, found {expected}")
    if int(report.get("verified_session_count", 0)) != expected:
        raise RuntimeError("Training blocked: verified archive count does not match exchange calendar")
    if int(report.get("feature_session_count", 0)) != expected:
        raise RuntimeError("Training blocked: parsed feature count does not match exchange calendar")
    if report.get("feature_error_count", 0):
        raise RuntimeError("Training blocked: one or more NIFTY feature rows failed validation")
    if len(checkpoint.get("features", {})) != expected:
        raise RuntimeError("Training blocked: checkpoint does not contain all 494 feature rows")
    if sorted(checkpoint["features"]) != sorted(report.get("verified_dates", [])):
        raise RuntimeError("Training blocked: feature dates do not exactly match integrity-verified sessions")
    for day, row in checkpoint["features"].items():
        if row.get("session_date") != day:
            raise RuntimeError(f"Training blocked: feature session key mismatch for {day}")
        if row.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
            raise RuntimeError(f"Training blocked: stale feature schema for {day}")
    feature_rows = list(checkpoint["features"].values())
    future_coverage = float(np.mean([
        float(row["features"].get("future_data_available", 0.0)) for row in feature_rows
    ]))
    options_coverage = float(np.mean([
        float(row["features"].get("ce_contract_count", 0.0)) > 0
        and float(row["features"].get("pe_contract_count", 0.0)) > 0
        for row in feature_rows
    ]))
    if future_coverage < 0.95 or options_coverage < 0.95:
        raise RuntimeError(
            "Training blocked: core NIFTY options/futures coverage is insufficient "
            f"(futures={future_coverage:.1%}, options={options_coverage:.1%}; need 95%)"
        )
    optional_coverage = {
        key: float(np.mean([float(row["features"].get(key, 0.0)) for row in feature_rows]))
        for key in ("index_data_available", "participant_oi_data_available", "participant_volume_data_available")
    }
    print("Optional context coverage (availability flags retained): " + json.dumps(optional_coverage), flush=True)
    return checkpoint, report


def build_daily_dataset(feature_rows: dict) -> tuple[pd.DataFrame, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray, list[str]]:
    ordered = sorted(feature_rows.values(), key=lambda row: row["session_date"])
    if len(ordered) < MIN_SESSIONS + 1:
        raise ValueError(f"Need at least {MIN_SESSIONS + 1} feature sessions; found {len(ordered)}")

    dates = [row["session_date"] for row in ordered]
    spot = np.array([float(row["features"]["spot_close"]) for row in ordered], dtype=float)
    if np.any(~np.isfinite(spot)) or np.any(spot <= 0):
        raise ValueError("Invalid spot close in feature dataset")

    rows = []
    labels = []
    returns = []
    large_moves = []
    breakouts = []
    traps = []
    upper_returns = []
    lower_returns = []
    for index in range(1, len(ordered) - 1):
        current = ordered[index]
        previous = ordered[index - 1]
        following = ordered[index + 1]
        current_features = {key: float(value) for key, value in current["features"].items()}
        current_features["spot_return_1d"] = spot[index] / spot[index - 1] - 1.0
        current_features["spot_return_5d"] = spot[index] / spot[index - 5] - 1.0 if index >= 5 else 0.0
        recent_returns = np.diff(spot[max(0, index - 5):index + 1]) / spot[max(0, index - 5):index]
        current_features["realized_volatility_5d"] = float(np.std(recent_returns)) if len(recent_returns) else 0.0
        recent_returns_20 = np.diff(spot[max(0, index - 20):index + 1]) / spot[max(0, index - 20):index]
        current_features["spot_return_20d"] = spot[index] / spot[index - 20] - 1.0 if index >= 20 else 0.0
        current_features["realized_volatility_20d"] = float(np.std(recent_returns_20)) if len(recent_returns_20) else 0.0
        current_features["india_vix_change_1d"] = (
            float(current["features"].get("india_vix_close", 0.0))
            - float(previous["features"].get("india_vix_close", 0.0))
        )
        rolling_keys = (
            "ce_writer_build", "pe_writer_build",
            "participant_oi_fii_future_index_net", "participant_oi_client_future_index_net",
            "participant_oi_fii_option_index_net", "participant_oi_client_option_index_net",
        )
        for rolling_key in rolling_keys:
            current_values = [
                float(item["features"].get(rolling_key, 0.0))
                for item in ordered[max(0, index - 19):index + 1]
            ]
            current_features[f"{rolling_key}_mean_5d"] = float(np.mean(current_values[-5:]))
            current_features[f"{rolling_key}_mean_20d"] = float(np.mean(current_values))
            current_features[f"{rolling_key}_delta_1d"] = (
                float(current["features"].get(rolling_key, 0.0))
                - float(previous["features"].get(rolling_key, 0.0))
            )
        current_features["ce_writer_build_delta_1d"] = (
            current_features.get("ce_writer_build", 0.0) - float(previous["features"].get("ce_writer_build", 0.0))
        )
        current_features["pe_writer_build_delta_1d"] = (
            current_features.get("pe_writer_build", 0.0) - float(previous["features"].get("pe_writer_build", 0.0))
        )
        next_return = spot[index + 1] / spot[index] - 1.0
        label = 2 if next_return > DIRECTION_THRESHOLD else 0 if next_return < -DIRECTION_THRESHOLD else 1
        rows.append(current_features)
        labels.append(label)
        returns.append(next_return)
        large_moves.append(int(abs(next_return) >= LARGE_MOVE_THRESHOLD))
        current_high = float(current["features"].get("spot_high", spot[index]))
        current_low = float(current["features"].get("spot_low", spot[index]))
        next_high = float(following["features"].get("spot_high", spot[index + 1]))
        next_low = float(following["features"].get("spot_low", spot[index + 1]))
        index_data_ok = (
            float(current["features"].get("index_data_available", 0.0)) > 0
            and float(following["features"].get("index_data_available", 0.0)) > 0
        )
        up_break = index_data_ok and next_high > current_high * 1.001
        down_break = index_data_ok and next_low < current_low * 0.999
        breakouts.append(int(up_break or down_break))
        traps.append(int((up_break and spot[index + 1] < current_high) or (down_break and spot[index + 1] > current_low)))
        upper_returns.append(next_high / spot[index] - 1.0)
        lower_returns.append(next_low / spot[index] - 1.0)

    frame = pd.DataFrame(rows).replace([np.inf, -np.inf], np.nan).fillna(0.0)
    feature_names = list(frame.columns)
    return (
        frame, np.asarray(labels, dtype=int), np.asarray(returns, dtype=float),
        np.asarray(large_moves, dtype=int), np.asarray(breakouts, dtype=int),
        np.asarray(traps, dtype=int), np.asarray(upper_returns, dtype=float),
        np.asarray(lower_returns, dtype=float), feature_names,
    )


def _probability_matrix(model, features: pd.DataFrame) -> np.ndarray:
    raw = model.predict_proba(features)
    aligned = np.zeros((len(features), 3), dtype=float)
    for index, label in enumerate(model.classes_):
        aligned[:, int(label)] = raw[:, index]
    row_sum = aligned.sum(axis=1, keepdims=True)
    return aligned / np.where(row_sum == 0, 1.0, row_sum)


def _multiclass_brier(y_true: np.ndarray, probabilities: np.ndarray) -> float:
    target = np.eye(3)[y_true]
    return float(np.mean(np.sum((probabilities - target) ** 2, axis=1)))


def _candidate_factories():
    return {
        "lightgbm": lambda: LGBMClassifier(
            objective="multiclass", num_class=3, n_estimators=180,
            learning_rate=0.025, num_leaves=7, max_depth=3,
            min_child_samples=18, reg_lambda=8.0, verbosity=-1,
            random_state=RANDOM_STATE,
        ),
        "catboost": lambda: CatBoostClassifier(
            iterations=220, depth=4, learning_rate=0.025,
            l2_leaf_reg=10, loss_function="MultiClass",
            auto_class_weights="Balanced", random_seed=RANDOM_STATE,
            verbose=False, allow_writing_files=False,
        ),
    }


def walk_forward_compare(X: pd.DataFrame, y: np.ndarray) -> tuple[str, dict]:
    splitter = TimeSeriesSplit(n_splits=4)
    results = {}
    for name, factory in _candidate_factories().items():
        fold_f1, fold_brier = [], []
        for train_index, test_index in splitter.split(X):
            y_train = y[train_index]
            if len(np.unique(y_train)) < 3:
                continue
            model = factory()
            model.fit(X.iloc[train_index], y_train)
            probabilities = _probability_matrix(model, X.iloc[test_index])
            predictions = np.argmax(probabilities, axis=1)
            fold_f1.append(float(f1_score(y[test_index], predictions, labels=[0, 1, 2], average="macro", zero_division=0)))
            fold_brier.append(_multiclass_brier(y[test_index], probabilities))
        if fold_f1:
            results[name] = {
                "walk_forward_macro_f1": float(np.mean(fold_f1)),
                "walk_forward_brier": float(np.mean(fold_brier)),
                "folds": len(fold_f1),
            }
    if not results:
        raise ValueError("No valid chronological folds contain all three market classes")
    selected = max(results, key=lambda name: (
        results[name]["walk_forward_macro_f1"],
        -results[name]["walk_forward_brier"],
    ))
    return selected, results


def build_contract_samples(feature_rows: dict) -> dict[str, tuple[pd.DataFrame, np.ndarray, list[str]]]:
    ordered = sorted(feature_rows.values(), key=lambda row: row["session_date"])
    outputs = {}
    for side in ("CE", "PE"):
        rows, targets, dates = [], [], []
        for index in range(len(ordered) - 1):
            current = ordered[index]
            following = ordered[index + 1]
            next_map = {
                (item["expiry"], float(item["strike"]), item["option_type"]): item
                for item in following.get("contracts", [])
            }
            daily = {key: float(value) for key, value in current["features"].items()}
            for contract in current.get("contracts", []):
                if contract["option_type"] != side:
                    continue
                key = (contract["expiry"], float(contract["strike"]), side)
                next_contract = next_map.get(key)
                close = float(contract["close"])
                if not next_contract or close <= 0 or float(next_contract["close"]) <= 0:
                    continue
                row = dict(daily)
                oi = float(contract["oi"])
                change_oi = float(contract["change_oi"])
                row.update({
                    "contract_moneyness": float(contract["moneyness"]),
                    "contract_days_to_expiry": float(contract["days_to_expiry"]),
                    "contract_premium_return": float(contract["premium_return"]),
                    "contract_delta_factor": float(contract.get("delta_factor", 0.0)),
                    "contract_delta_oi": float(contract.get("delta_factor", 0.0)) * oi,
                    "contract_oi_log1p": float(np.log1p(max(0.0, oi))),
                    "contract_volume_log1p": float(np.log1p(max(0.0, float(contract["volume"])))),
                    "contract_change_oi_pct": change_oi / max(1.0, oi - change_oi),
                    "is_ce": 1.0 if side == "CE" else 0.0,
                    "spot_return_1d": float(current["features"]["spot_close"]) / float(ordered[index - 1]["features"]["spot_close"]) - 1.0 if index else 0.0,
                })
                rows.append(row)
                targets.append((close - float(next_contract["close"])) / close)
                dates.append(current["session_date"])
        if rows:
            frame = pd.DataFrame(rows).replace([np.inf, -np.inf], np.nan).fillna(0.0)
            outputs[side] = (frame, np.asarray(targets, dtype=float), dates)
    return outputs


def train_pipeline(checkpoint_path: Path, output_dir: Path) -> dict:
    checkpoint, audit = check_training_gate(checkpoint_path)
    data_hash = _canonical_hash(checkpoint["features"])
    script_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    manifest_path = output_dir / "model_manifest.json"
    model_path = output_dir / "nifty_adaptive_v2.joblib"
    if manifest_path.is_file() and model_path.is_file():
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
        if (
            existing.get("dataset_sha256") == data_hash
            and existing.get("training_script_sha256") == script_hash
            and existing.get("training_pipeline_version") == TRAINING_PIPELINE_VERSION
            and existing.get("model_sha256") == model_hash
        ):
            return {**existing, "status": "already_trained_for_current_dataset"}
    _load_ml_dependencies()
    feature_rows = checkpoint["features"]
    X, y, next_returns, large_move_labels, breakout_labels, trap_labels, upper_returns, lower_returns, feature_names = build_daily_dataset(feature_rows)
    if len(X) < MIN_SESSIONS:
        raise ValueError(f"Only {len(X)} labelled daily rows; at least {MIN_SESSIONS} required")
    class_counts = {DIRECTION_LABELS[int(key)]: int(value) for key, value in zip(*np.unique(y, return_counts=True))}
    if min(class_counts.values()) < MIN_CLASS_SAMPLES:
        raise ValueError(f"Insufficient class coverage for stable 3-way training: {class_counts}")

    split = int(len(X) * 0.8)
    X_train, X_test = X.iloc[:split], X.iloc[split:]
    y_train, y_test = y[:split], y[split:]
    selected, walk_forward = walk_forward_compare(X_train, y_train)
    factory = _candidate_factories()[selected]
    holdout_model = factory()
    holdout_model.fit(X_train, y_train)
    raw_probabilities = _probability_matrix(holdout_model, X_test)
    raw_predictions = np.argmax(raw_probabilities, axis=1)
    holdout_f1 = float(f1_score(y_test, raw_predictions, labels=[0, 1, 2], average="macro", zero_division=0))
    holdout_brier = _multiclass_brier(y_test, raw_probabilities)
    baseline = DummyClassifier(strategy="prior")
    baseline.fit(X_train, y_train)
    baseline_probabilities = _probability_matrix(baseline, X_test)
    baseline_predictions = np.argmax(baseline_probabilities, axis=1)
    baseline_f1 = float(f1_score(y_test, baseline_predictions, labels=[0, 1, 2], average="macro", zero_division=0))
    baseline_brier = _multiclass_brier(y_test, baseline_probabilities)

    calibration_status = "not_attempted"
    calibration_error = None
    calibrated = None
    try:
        calibrated = CalibratedClassifierCV(
            estimator=factory(), method="sigmoid", cv=TimeSeriesSplit(n_splits=3), ensemble=True
        )
        calibrated.fit(X_train, y_train)
        calibrated_probabilities = _probability_matrix(calibrated, X_test)
        calibrated_predictions = np.argmax(calibrated_probabilities, axis=1)
        holdout_f1 = float(f1_score(y_test, calibrated_predictions, labels=[0, 1, 2], average="macro", zero_division=0))
        holdout_brier = _multiclass_brier(y_test, calibrated_probabilities)
        calibration_status = "time_series_sigmoid_calibration"
    except Exception as exc:
        calibration_status = f"failed:{type(exc).__name__}"
        calibration_error = f"{type(exc).__name__}: {str(exc)[:500]}"

    final_direction = CalibratedClassifierCV(
        estimator=factory(), method="sigmoid", cv=TimeSeriesSplit(n_splits=3), ensemble=True
    )
    try:
        final_direction.fit(X, y)
    except Exception as exc:
        calibration_error = f"final_calibration:{type(exc).__name__}: {str(exc)[:500]}"
        final_direction = factory()
        final_direction.fit(X, y)
        calibration_status = "uncalibrated_fallback"

    range_models = {}
    range_metrics = {}
    for quantile in (0.1, 0.5, 0.9):
        model = HistGradientBoostingRegressor(
            loss="quantile", quantile=quantile, max_iter=120,
            max_leaf_nodes=7, l2_regularization=8.0, random_state=RANDOM_STATE,
        )
        model.fit(X_train, next_returns[:split])
        predictions = model.predict(X_test)
        range_metrics[f"close_q{int(quantile * 100)}"] = float(pinball_loss(next_returns[split:], predictions, alpha=quantile))
        model.fit(X, next_returns)
        range_models[f"close_q{int(quantile * 100)}"] = model

    upper_model = HistGradientBoostingRegressor(
        loss="quantile", quantile=0.9, max_iter=120,
        max_leaf_nodes=7, l2_regularization=8.0, random_state=RANDOM_STATE,
    )
    upper_model.fit(X_train, upper_returns[:split])
    range_metrics["upper_q90"] = float(pinball_loss(upper_returns[split:], upper_model.predict(X_test), alpha=0.9))
    upper_model.fit(X, upper_returns)
    range_models["upper_q90"] = upper_model

    lower_model = HistGradientBoostingRegressor(
        loss="quantile", quantile=0.1, max_iter=120,
        max_leaf_nodes=7, l2_regularization=8.0, random_state=RANDOM_STATE,
    )
    lower_model.fit(X_train, lower_returns[:split])
    range_metrics["lower_q10"] = float(pinball_loss(lower_returns[split:], lower_model.predict(X_test), alpha=0.1))
    lower_model.fit(X, lower_returns)
    range_models["lower_q10"] = lower_model

    def fit_binary_event_model(labels: np.ndarray):
        if len(np.unique(labels)) > 1:
            model = LGBMClassifier(
                objective="binary", n_estimators=120, learning_rate=0.03,
                num_leaves=7, max_depth=3, min_child_samples=18,
                reg_lambda=8.0, verbosity=-1, random_state=RANDOM_STATE,
            )
        else:
            model = DummyClassifier(strategy="prior")
        model.fit(X, labels)
        return model

    large_move_model = fit_binary_event_model(large_move_labels)
    breakout_model = fit_binary_event_model(breakout_labels)
    trap_model = fit_binary_event_model(trap_labels)

    seller_models = {}
    seller_metrics = {}
    contract_samples = build_contract_samples(feature_rows)
    for side, (contract_X, contract_y, contract_dates) in contract_samples.items():
        ordered_unique_dates = sorted(set(contract_dates))
        cutoff_index = max(1, int(len(ordered_unique_dates) * 0.8))
        cutoff_date = ordered_unique_dates[cutoff_index]
        train_mask = np.asarray([day < cutoff_date for day in contract_dates])
        test_mask = ~train_mask
        if int(train_mask.sum()) < 200 or int(test_mask.sum()) < 30:
            seller_metrics[side] = {"status": "insufficient_contract_samples", "samples": len(contract_X)}
            continue
        regressor = LGBMRegressor(
            objective="regression_l1", n_estimators=160, learning_rate=0.025,
            num_leaves=7, max_depth=3, min_child_samples=20,
            reg_lambda=8.0, verbosity=-1, random_state=RANDOM_STATE,
        )
        regressor.fit(contract_X.loc[train_mask], contract_y[train_mask])
        holdout_prediction = regressor.predict(contract_X.loc[test_mask])
        mae = float(mean_absolute_error(contract_y[test_mask], holdout_prediction))
        regressor.fit(contract_X, contract_y)
        seller_models[side] = regressor
        seller_metrics[side] = {
            "status": "trained",
            "samples": len(contract_X),
            "holdout_samples": int(test_mask.sum()),
            "holdout_mae": mae,
            "feature_names": list(contract_X.columns),
        }

    scaler = StandardScaler()
    scaled = scaler.fit_transform(X)
    regime_model = GaussianMixture(n_components=3, covariance_type="diag", reg_covar=1e-4, random_state=RANDOM_STATE)
    regime_model.fit(scaled)
    similarity_model = NearestNeighbors(n_neighbors=min(10, len(X)), metric="euclidean")
    similarity_model.fit(scaled)

    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / "nifty_adaptive_v2.joblib"
    deployment_ready = (
        holdout_f1 >= baseline_f1 + 0.03
        and holdout_brier < baseline_brier
        and calibration_status == "time_series_sigmoid_calibration"
        and all(side in seller_models for side in ("CE", "PE"))
    )
    bundle = {
        "model_version": "NIFTY-Adaptive-Intelligence-V2",
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "feature_names": feature_names,
        "direction_model": final_direction,
        "range_models": range_models,
        "large_move_model": large_move_model,
        "breakout_model": breakout_model,
        "trap_model": trap_model,
        "seller_models": seller_models,
        "seller_feature_names": {side: metrics.get("feature_names", []) for side, metrics in seller_metrics.items()},
        "regime_scaler": scaler,
        "regime_model": regime_model,
        "similarity_scaler": scaler,
        "similarity_model": similarity_model,
        "direction_labels": DIRECTION_LABELS,
        "label_thresholds": {
            "direction_return": DIRECTION_THRESHOLD,
            "large_move_return": LARGE_MOVE_THRESHOLD,
        },
    }
    joblib.dump(bundle, model_path, compress=3)

    manifest = {
        "model_version": "NIFTY-Adaptive-Intelligence-V2",
        "training_pipeline_version": TRAINING_PIPELINE_VERSION,
        "training_script_sha256": script_hash,
        "status": "trained_candidate" if not deployment_ready else "validated_candidate",
        "deployment_ready": bool(deployment_ready),
        "model_file": model_path.name,
        "model_sha256": hashlib.sha256(model_path.read_bytes()).hexdigest(),
        "dataset_sha256": data_hash,
        "audit_status": audit.get("status"),
        "session_count": audit.get("verified_session_count"),
        "feature_count": audit.get("feature_session_count"),
        "labelled_daily_samples": len(X),
        "training_start": min(row["session_date"] for row in feature_rows.values()),
        "training_end": max(row["session_date"] for row in feature_rows.values()),
        "holdout_start": min(sorted(set(row["session_date"] for row in sorted(feature_rows.values(), key=lambda x: x["session_date"])))[split:]),
        "selected_direction_model": selected,
        "calibration_error": calibration_error,
        "walk_forward_comparison": walk_forward,
        "direction_holdout": {
            "macro_f1": holdout_f1,
            "multiclass_brier": holdout_brier,
            "baseline_macro_f1": baseline_f1,
            "baseline_brier": baseline_brier,
            "class_counts": class_counts,
            "calibration": calibration_status,
        },
        "range_holdout_pinball_loss": range_metrics,
        "large_move_positive_rate": float(np.mean(large_move_labels)),
        "breakout_positive_rate": float(np.mean(breakout_labels)),
        "trap_positive_rate": float(np.mean(trap_labels)),
        "core_market_coverage": {
            "future_data_available": float(np.mean([float(row["features"].get("future_data_available", 0.0)) for row in feature_rows.values()])),
            "nifty_options_available": float(np.mean([
                float(row["features"].get("ce_contract_count", 0.0)) > 0
                and float(row["features"].get("pe_contract_count", 0.0)) > 0
                for row in feature_rows.values()
            ])),
        },
        "index_data_coverage": float(np.mean([float(row["features"].get("index_data_available", 0.0)) for row in feature_rows.values()])),
        "participant_oi_coverage": float(np.mean([float(row["features"].get("participant_oi_data_available", 0.0)) for row in feature_rows.values()])),
        "participant_volume_coverage": float(np.mean([float(row["features"].get("participant_volume_data_available", 0.0)) for row in feature_rows.values()])),
        "contract_delta_data_coverage": float(np.mean([float(row["features"].get("delta_data_available", 0.0)) for row in feature_rows.values()])),
        "contract_delta_match_coverage": float(np.mean([float(row["features"].get("delta_match_coverage", 0.0)) for row in feature_rows.values()])),
        "seller_models": seller_metrics,
        "deployment_gate": {
            "beats_baseline_macro_f1_by": 0.03,
            "beats_baseline_brier": True,
            "requires_time_series_calibration": True,
            "requires_both_seller_models": True,
        },
        "data_scope": {
            "nifty_options_and_futures": "NIFTY contracts from NSE UDiFF bhavcopy",
            "participant_positioning": "NSE-wide index derivatives participant totals; contextual, not NIFTY-only",
            "volatility": "India VIX from NSE index close report when present; not option-chain implied volatility",
        },
        "disclaimer": "Research model candidate; no live trading or guaranteed accuracy.",
    }
    (output_dir / "model_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Train the NIFTY Adaptive Intelligence V2 candidate after the historical audit gate passes.")
    parser.add_argument("--checkpoint", default="data/historical_audit/checkpoint.json")
    parser.add_argument("--output-dir", default="models/artifacts")
    args = parser.parse_args()
    import os
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write("trained=false\n")
    try:
        result = train_pipeline(Path(args.checkpoint), Path(args.output_dir))
    except RuntimeError as exc:
        print(str(exc), flush=True)
        raise SystemExit(0)
    print(json.dumps(result, indent=2), flush=True)
    if output_path:
        trained = "false" if result.get("status") == "already_trained_for_current_dataset" else "true"
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"trained={trained}\n")
