from __future__ import annotations

import argparse
import hashlib
import json
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd


def _load_training_dependencies() -> None:
    """Load heavy ML dependencies only inside the explicit training workflow."""
    global joblib, LGBMClassifier, LGBMRegressor
    global DummyClassifier, DummyRegressor, RandomForestRegressor
    global f1_score, mean_absolute_error, r2_score, TimeSeriesSplit
    import joblib
    from lightgbm import LGBMClassifier, LGBMRegressor
    from sklearn.dummy import DummyClassifier, DummyRegressor
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.metrics import f1_score, mean_absolute_error, r2_score
    from sklearn.model_selection import TimeSeriesSplit


FEATURE_SCHEMA_VERSION = 3
PIPELINE_VERSION = 2
EXPECTED_SESSIONS = 494
DIRECTION_THRESHOLD = 0.0025
LABELS = {0: "bearish", 1: "neutral", 2: "bullish"}
RANDOM_STATE = 42

# Features are available after the session close. The rolling windows include
# the latest completed session (the forecast origin) and look backwards only.
ROLLING_SOURCE_KEYS = (
    "index_range_pct", "index_close_location", "india_vix_close", "days_to_expiry",
    "future_return", "future_basis_pct", "future_oi_change_pct",
    "pcr_oi", "pcr_change_oi", "pcr_volume",
    "ce_premium_return", "pe_premium_return",
    "ce_writer_build_share", "pe_writer_build_share",
    "ce_max_oi_strike_distance", "pe_max_oi_strike_distance",
    "all_expiry_pcr_oi", "delta_match_coverage",
    "participant_oi_fii_future_index_net", "participant_oi_client_future_index_net",
    "participant_oi_fii_option_index_call_net", "participant_oi_fii_option_index_put_net",
    "participant_oi_client_option_index_call_net", "participant_oi_client_option_index_put_net",
    "participant_volume_fii_option_index_call_net", "participant_volume_fii_option_index_put_net",
    "participant_volume_client_option_index_call_net", "participant_volume_client_option_index_put_net",
)
STD_SOURCE_KEYS = (
    "index_range_pct", "india_vix_close", "future_return", "pcr_oi", "pcr_volume",
    "ce_premium_return", "pe_premium_return", "ce_writer_build_share", "pe_writer_build_share",
    "spot_return_1d",
)
REQUIRED_AVAILABILITY = (
    "index_data_available", "participant_oi_data_available", "participant_volume_data_available",
    "future_data_available",
)

# A compact domain-selected set addresses the first pass's 253 inputs versus
# 377 daily training observations. It is shared across train and inference.
MODEL_CORE_SOURCE_KEYS = (
    "india_vix_close", "days_to_expiry", "future_return", "future_basis_pct",
    "future_oi_change_pct", "pcr_oi", "pcr_change_oi", "pcr_volume",
    "ce_premium_return", "pe_premium_return", "ce_writer_build_share",
    "pe_writer_build_share", "ce_max_oi_strike_distance", "pe_max_oi_strike_distance",
    "participant_oi_fii_future_index_net", "participant_oi_client_future_index_net",
    "participant_oi_fii_option_index_call_net", "participant_oi_fii_option_index_put_net",
    "participant_oi_client_option_index_call_net", "participant_oi_client_option_index_put_net",
    "participant_volume_fii_option_index_call_net", "participant_volume_fii_option_index_put_net",
    "participant_volume_client_option_index_call_net", "participant_volume_client_option_index_put_net",
)


def _select_model_feature_vector(values: dict[str, float]) -> dict[str, float]:
    selected = {
        key: value for key, value in values.items()
        if key.startswith((
            "spot_return_", "spot_open_gap", "spot_candle", "spot_intraday",
            "spot_close_location", "realized_volatility", "high_distance", "low_distance",
        ))
        or any(key in (f"latest__{source}", f"mean5__{source}", f"change5__{source}")
               for source in MODEL_CORE_SOURCE_KEYS)
    }
    if not selected:
        raise ValueError("Selected model feature vector is empty")
    if not all(math.isfinite(float(value)) for value in selected.values()):
        raise ValueError("Selected model feature vector contains non-finite values")
    return selected


def _signed_log(value: float) -> float:
    return math.copysign(math.log1p(abs(value)), value) if value else 0.0


def _required_number(features: dict[str, Any], key: str, day: str) -> float:
    if key not in features or features[key] is None:
        raise ValueError(f"{day}: required source feature {key!r} is missing; no imputation is permitted")
    value = float(features[key])
    if not math.isfinite(value):
        raise ValueError(f"{day}: required source feature {key!r} is not finite")
    return value


def _base_session_vector(row: dict[str, Any], previous_close: float | None) -> dict[str, float]:
    day = str(row.get("session_date"))
    f = row.get("features", {})
    vector: dict[str, float] = {}
    close = _required_number(f, "spot_close", day)
    if close <= 0:
        raise ValueError(f"{day}: spot_close must be positive")
    open_price = _required_number(f, "spot_open", day)
    high_price = _required_number(f, "spot_high", day)
    low_price = _required_number(f, "spot_low", day)
    reference_close = previous_close if previous_close else close
    vector["spot_return_1d"] = (close / previous_close - 1.0) if previous_close else 0.0
    vector["spot_open_gap_pct"] = (open_price / previous_close - 1.0) if previous_close else 0.0
    vector["spot_candle_body_pct"] = (close - open_price) / reference_close
    vector["spot_intraday_range_pct"] = (high_price - low_price) / reference_close
    vector["spot_close_location"] = (
        (close - low_price) / (high_price - low_price) if high_price > low_price else 0.5
    )

    direct_keys = (
        "index_range_pct", "index_close_location", "india_vix_close", "days_to_expiry",
        "future_return", "future_basis_pct", "future_oi_change_pct",
        "pcr_oi", "pcr_change_oi", "pcr_volume",
        "ce_premium_return", "pe_premium_return",
        "ce_writer_build_share", "pe_writer_build_share",
        "ce_max_oi_strike_distance", "pe_max_oi_strike_distance",
        "all_expiry_pcr_oi", "delta_match_coverage",
    ) + tuple(key for key in ROLLING_SOURCE_KEYS if key.startswith("participant_"))
    for key in direct_keys:
        value = _required_number(f, key, day)
        # Participant positioning counts are large and signed; signed-log
        # scaling preserves direction while stabilizing magnitude.
        if key.startswith("participant_"):
            value = _signed_log(value)
        vector[key] = value

    # Availability flags are mandatory quality features, not values to impute.
    for key in REQUIRED_AVAILABILITY:
        value = _required_number(f, key, day)
        if value < 1.0:
            raise ValueError(f"{day}: {key} is not available; training gate blocks rather than filling a gap")
        vector[key] = value

    # Add stable logarithmic activity measures derived from observed values.
    for source_key, output_key in (
        ("ce_oi", "ce_oi_log"), ("pe_oi", "pe_oi_log"),
        ("ce_volume", "ce_volume_log"), ("pe_volume", "pe_volume_log"),
        ("future_volume", "future_volume_log"),
        ("ce_writer_build", "ce_writer_build_log"),
        ("pe_writer_build", "pe_writer_build_log"),
        ("ce_short_cover", "ce_short_cover_log"),
        ("pe_short_cover", "pe_short_cover_log"),
        ("ce_long_unwind", "ce_long_unwind_log"),
        ("pe_long_unwind", "pe_long_unwind_log"),
        ("net_option_delta_equiv_oi", "net_option_delta_equiv_oi_log"),
    ):
        value = _required_number(f, source_key, day)
        vector[output_key] = _signed_log(value)

    return vector


def _build_window_feature_vector(
    window22: list[dict[str, float]],
    close22: list[float],
    high22: list[float],
    low22: list[float],
) -> dict[str, float]:
    """Shared rolling calculation used by both historical training and inference."""
    if len(window22) != 22 or len(close22) != 22 or len(high22) != 22 or len(low22) != 22:
        raise ValueError("Rolling feature engine requires exactly 22 completed sessions")
    window5 = window22[-5:]
    values: dict[str, float] = {}
    for key in window22[-1]:
        five = np.asarray([r[key] for r in window5], dtype=float)
        month = np.asarray([r[key] for r in window22], dtype=float)
        values[f"latest__{key}"] = float(five[-1])
        values[f"mean5__{key}"] = float(five.mean())
        values[f"mean22__{key}"] = float(month.mean())
        values[f"change5__{key}"] = float(five[-1] - five[0])
        values[f"change22__{key}"] = float(month[-1] - month[0])
        if key in STD_SOURCE_KEYS or key == "spot_return_1d":
            values[f"std5__{key}"] = float(five.std(ddof=0))
            values[f"std22__{key}"] = float(month.std(ddof=0))

    values["spot_return_5_sessions"] = close22[-1] / close22[-5] - 1.0
    values["spot_return_22_sessions"] = close22[-1] / close22[0] - 1.0
    recent_returns5 = np.diff(close22[-5:]) / np.asarray(close22[-5:-1])
    recent_returns22 = np.diff(close22) / np.asarray(close22[:-1])
    values["realized_volatility_5_sessions"] = float(np.std(recent_returns5, ddof=0))
    values["realized_volatility_22_sessions"] = float(np.std(recent_returns22, ddof=0))
    values["high_distance_5_sessions"] = max(high22[-5:]) / close22[-1] - 1.0
    values["low_distance_5_sessions"] = min(low22[-5:]) / close22[-1] - 1.0
    values["high_distance_22_sessions"] = max(high22) / close22[-1] - 1.0
    values["low_distance_22_sessions"] = min(low22) / close22[-1] - 1.0
    if not all(math.isfinite(v) for v in values.values()):
        raise ValueError("Rolling feature vector contains non-finite values")
    return values


def _raw_session_vectors(ordered: list[dict[str, Any]]) -> tuple[list[dict[str, float]], list[float], list[float], list[float]]:
    raw_vectors: list[dict[str, float]] = []
    closes: list[float] = []
    highs: list[float] = []
    lows: list[float] = []
    for row in ordered:
        day = row.get("session_date")
        f = row.get("features", {})
        close = _required_number(f, "spot_close", day)
        high = _required_number(f, "spot_high", day)
        low = _required_number(f, "spot_low", day)
        if close <= 0 or high < close or low > close or low <= 0:
            raise ValueError(f"{day}: invalid NIFTY spot OHLC values")
        previous_close = closes[-1] if closes else None
        closes.append(close)
        highs.append(high)
        lows.append(low)
        raw_vectors.append(_base_session_vector(row, previous_close))
    return raw_vectors, closes, highs, lows


def build_latest_inference_features(feature_rows: dict[str, dict[str, Any]]) -> dict[str, float]:
    """Create the current forecast input from stored history (23 rows needed to derive 1-day returns)."""
    ordered_dates = sorted(feature_rows)
    if len(ordered_dates) < 23:
        raise ValueError("Daily inference requires at least 23 stored sessions to build a complete 22-session window")
    ordered = [feature_rows[d] for d in ordered_dates]
    for day, row in zip(ordered_dates, ordered):
        if row.get("session_date") != day:
            raise ValueError(f"Feature key/date mismatch for {day}")
    raw_vectors, closes, highs, lows = _raw_session_vectors(ordered)
    result = _build_window_feature_vector(
        raw_vectors[-22:], closes[-22:], highs[-22:], lows[-22:]
    )
    result = _select_model_feature_vector(result)
    if len(result) == 0:
        raise ValueError("Daily inference feature vector is empty")
    return result


def build_rolling_feature_rows(feature_rows: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Build one input vector per forecast origin using trailing 5/22 sessions only.

    The 5- and 22-session windows both include the latest completed source session.
    A model sample predicts the following session, never the origin session.
    """
    ordered_dates = sorted(feature_rows)
    if len(ordered_dates) != EXPECTED_SESSIONS:
        raise ValueError(f"Expected exactly {EXPECTED_SESSIONS} verified session rows; got {len(ordered_dates)}")
    ordered = [feature_rows[d] for d in ordered_dates]
    for day, row in zip(ordered_dates, ordered):
        if row.get("session_date") != day:
            raise ValueError(f"Feature key/date mismatch for {day}")
    raw_vectors, closes, highs, lows = _raw_session_vectors(ordered)

    outputs: list[dict[str, Any]] = []
    for i in range(21, len(ordered) - 1):
        values = _build_window_feature_vector(
            raw_vectors[i - 21:i + 1],
            closes[i - 21:i + 1],
            highs[i - 21:i + 1],
            lows[i - 21:i + 1],
        )
        outputs.append({
            "origin_date": ordered[i]["session_date"],
            "target_date": ordered[i + 1]["session_date"],
            "features": values,
            "origin_index": i,
        })
    return outputs


def _feature_matrix(samples: list[dict[str, Any]]) -> tuple[pd.DataFrame, list[str]]:
    frame = pd.DataFrame([
        _select_model_feature_vector(item["features"]) for item in samples
    ]).astype(float)
    if frame.isna().any().any() or not np.isfinite(frame.to_numpy()).all():
        raise ValueError("Rolling feature matrix contains missing/non-finite values; no automatic filling is allowed")
    return frame, list(frame.columns)


def _build_daily_targets(samples: list[dict[str, Any]], feature_rows: dict[str, dict[str, Any]]) -> dict[str, np.ndarray]:
    daily_targets = {"direction": [], "close_return": [], "low_return": [], "high_return": []}
    for sample in samples:
        origin = feature_rows[sample["origin_date"]]["features"]
        following = feature_rows[sample["target_date"]]["features"]
        current_close = _required_number(origin, "spot_close", sample["origin_date"])
        next_close = _required_number(following, "spot_close", sample["target_date"])
        next_low = _required_number(following, "spot_low", sample["target_date"])
        next_high = _required_number(following, "spot_high", sample["target_date"])
        close_return = next_close / current_close - 1.0
        daily_targets["direction"].append(2 if close_return > DIRECTION_THRESHOLD else 0 if close_return < -DIRECTION_THRESHOLD else 1)
        daily_targets["close_return"].append(close_return)
        daily_targets["low_return"].append(next_low / current_close - 1.0)
        daily_targets["high_return"].append(next_high / current_close - 1.0)
    return {key: np.asarray(value, dtype=float if key != "direction" else int) for key, value in daily_targets.items()}


def _contract_feature_vector(session_features: dict[str, float], contract: dict[str, Any], side: str) -> dict[str, float]:
    result = _select_model_feature_vector(session_features)
    close = float(contract["close"])
    oi = float(contract["oi"])
    volume = float(contract["volume"])
    change_oi = float(contract["change_oi"])
    result.update({
        "contract_moneyness": float(contract["moneyness"]),
        "contract_days_to_expiry": float(contract["days_to_expiry"]),
        "contract_premium_return": float(contract["premium_return"]),
        "contract_delta_factor": float(contract.get("delta_factor", 0.0)),
        "contract_oi_log": math.log1p(max(0.0, oi)),
        "contract_volume_log": math.log1p(max(0.0, volume)),
        "contract_change_oi_pct": change_oi / max(1.0, oi - change_oi),
        "contract_is_ce": 1.0 if side == "CE" else 0.0,
        "contract_close_log": math.log1p(max(0.0, close)),
    })
    if not all(math.isfinite(value) for value in result.values()):
        raise ValueError("Contract-level feature contains non-finite value")
    return result


def build_seller_samples(
    feature_rows: dict[str, dict[str, Any]], rolling_samples: list[dict[str, Any]], side: str
) -> tuple[pd.DataFrame, np.ndarray, list[str], list[str]]:
    by_origin = {item["origin_date"]: item for item in rolling_samples}
    ordered_dates = sorted(feature_rows)
    rows: list[dict[str, float]] = []
    targets: list[float] = []
    dates: list[str] = []
    for i in range(21, len(ordered_dates) - 1):
        day, next_day = ordered_dates[i], ordered_dates[i + 1]
        current_row, next_row = feature_rows[day], feature_rows[next_day]
        sample = by_origin[day]
        next_contracts = {
            (str(c["expiry"]), round(float(c["strike"]), 2), str(c["option_type"]).upper()): c
            for c in next_row.get("contracts", [])
        }
        for contract in current_row.get("contracts", []):
            if str(contract.get("option_type", "")).upper() != side:
                continue
            key = (str(contract["expiry"]), round(float(contract["strike"]), 2), side)
            following = next_contracts.get(key)
            current_premium = float(contract["close"])
            next_premium = float(following["close"]) if following else 0.0
            if following is None or current_premium <= 0 or next_premium <= 0:
                continue
            rows.append(_contract_feature_vector(sample["features"], contract, side))
            # Positive means the option premium decayed, which is a seller-side
            # theoretical premium move before costs/hedging (not realized P&L).
            targets.append((current_premium - next_premium) / current_premium)
            dates.append(day)
    if not rows:
        raise ValueError(f"No matched next-session {side} contract samples were built")
    frame = pd.DataFrame(rows).astype(float)
    if frame.isna().any().any() or not np.isfinite(frame.to_numpy()).all():
        raise ValueError(f"{side} seller feature matrix contains missing/non-finite values")
    return frame, np.asarray(targets, dtype=float), list(frame.columns), dates


def _check_artifact_gates(checkpoint_path: Path, audit_path: Path, repair_path: Path) -> tuple[dict, dict, dict]:
    for path in (checkpoint_path, audit_path, repair_path):
        if not path.is_file():
            raise RuntimeError(f"Training blocked: required verified input artifact is missing: {path}")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    repair = json.loads(repair_path.read_text(encoding="utf-8"))
    if audit.get("status") != "archive_integrity_complete_calendar_reconciled":
        raise RuntimeError("Training blocked: historical archive integrity status is not complete")
    if audit.get("model_training_allowed") is not True:
        raise RuntimeError("Training blocked: archive-level core-data gate is not open")
    if any(int(audit.get(key, -1)) != EXPECTED_SESSIONS for key in (
        "expected_session_count", "verified_session_count", "feature_session_count"
    )):
        raise RuntimeError("Training blocked: audit must report 494 expected, verified and feature rows")
    if int(audit.get("feature_error_count", -1)) != 0:
        raise RuntimeError("Training blocked: historical feature errors exist")
    if repair.get("repair_complete") is not True or repair.get("training_allowed") is not True:
        raise RuntimeError("Training blocked: final context repair/training gate is not PASS")
    if int(repair.get("date_count", 0)) != EXPECTED_SESSIONS:
        raise RuntimeError("Training blocked: context repair date count is not 494")
    if repair.get("date_alignment_pass") is not True:
        raise RuntimeError("Training blocked: context report date alignment did not PASS")
    if repair.get("feature_validation", {}).get("pass") is not True:
        raise RuntimeError("Training blocked: repaired feature validation did not PASS")
    if int(repair.get("feature_validation", {}).get("error_count", -1)) != 0:
        raise RuntimeError("Training blocked: repaired feature validation has errors")
    for source, coverage in repair.get("coverage", {}).items():
        if float(coverage) < 0.95:
            raise RuntimeError(f"Training blocked: {source} context coverage below 95%")
    if int(repair.get("b2_requests", -1)) != 0:
        raise RuntimeError("Training blocked: artifact reports non-zero B2 API calls")
    features = checkpoint.get("features", {})
    if len(features) != EXPECTED_SESSIONS:
        raise RuntimeError("Training blocked: checkpoint does not contain 494 feature rows")
    if sorted(features) != sorted(audit.get("verified_dates", [])):
        raise RuntimeError("Training blocked: checkpoint dates do not equal verified archive dates")
    return checkpoint, audit, repair


def _direction_metrics(y_true: np.ndarray, probabilities: np.ndarray) -> dict[str, float]:
    from sklearn.metrics import f1_score
    pred = np.argmax(probabilities, axis=1)
    onehot = np.eye(3)[y_true]
    return {
        "macro_f1": float(f1_score(y_true, pred, labels=[0, 1, 2], average="macro", zero_division=0)),
        "multiclass_brier": float(np.mean(np.sum((probabilities - onehot) ** 2, axis=1))),
        "accuracy": float(np.mean(pred == y_true)),
    }


def train_four_models(checkpoint_path: Path, audit_path: Path, repair_path: Path, output_dir: Path) -> dict:
    _load_training_dependencies()
    import lightgbm
    import platform
    import sklearn
    checkpoint, audit, repair = _check_artifact_gates(checkpoint_path, audit_path, repair_path)
    feature_rows = checkpoint["features"]
    rolling_samples = build_rolling_feature_rows(feature_rows)
    X_daily, feature_names = _feature_matrix(rolling_samples)
    daily_targets = _build_daily_targets(rolling_samples, feature_rows)
    dates = [item["origin_date"] for item in rolling_samples]
    split = int(len(X_daily) * 0.80)
    if split < 250 or len(X_daily) - split < 50:
        raise ValueError(f"Insufficient chronological split size: total={len(X_daily)}, split={split}")
    X_train, X_test = X_daily.iloc[:split], X_daily.iloc[split:]
    y_direction = daily_targets["direction"]
    y_train, y_test = y_direction[:split], y_direction[split:]
    if set(np.unique(y_train)) != {0, 1, 2}:
        raise ValueError(f"Direction training section lacks all 3 classes: {np.unique(y_train).tolist()}")

    # Model 1: next-session direction classifier.
    direction_model = LGBMClassifier(
        objective="multiclass", num_class=3, class_weight="balanced",
        n_estimators=120, learning_rate=0.025, num_leaves=5, max_depth=2,
        min_child_samples=25, reg_lambda=15.0, reg_alpha=2.0,
        colsample_bytree=0.7, subsample=0.8, subsample_freq=1,
        verbosity=-1, random_state=RANDOM_STATE, n_jobs=2,
    )
    direction_model.fit(X_train, y_train)
    test_prob = direction_model.predict_proba(X_test)
    classes = list(direction_model.classes_)
    aligned_prob = np.zeros((len(X_test), 3), dtype=float)
    for j, label in enumerate(classes):
        aligned_prob[:, int(label)] = test_prob[:, j]
    direction_probability_blend = 0.5
    direction_train_prior = np.bincount(y_train, minlength=3).astype(float) / len(y_train)
    aligned_prob = (
        direction_probability_blend * aligned_prob
        + (1.0 - direction_probability_blend) * direction_train_prior
    )
    direction_holdout = _direction_metrics(y_test, aligned_prob)
    direction_baseline = DummyClassifier(strategy="prior")
    direction_baseline.fit(X_train, y_train)
    base_prob = np.zeros((len(X_test), 3), dtype=float)
    for j, label in enumerate(direction_baseline.classes_):
        base_prob[:, int(label)] = direction_baseline.predict_proba(X_test)[:, j]
    direction_baseline_metrics = _direction_metrics(y_test, base_prob)
    splitter = TimeSeriesSplit(n_splits=3)
    walk_forward = []
    for fold, (tr, va) in enumerate(splitter.split(X_train), start=1):
        if len(np.unique(y_train[tr])) < 3:
            continue
        fold_model = LGBMClassifier(
            objective="multiclass", num_class=3, class_weight="balanced",
            n_estimators=120, learning_rate=0.025, num_leaves=5, max_depth=2,
            min_child_samples=25, reg_lambda=15.0, reg_alpha=2.0,
            colsample_bytree=0.7, subsample=0.8, subsample_freq=1,
            verbosity=-1, random_state=RANDOM_STATE, n_jobs=2,
        )
        fold_model.fit(X_train.iloc[tr], y_train[tr])
        fold_raw = fold_model.predict_proba(X_train.iloc[va])
        fold_prob = np.zeros((len(va), 3), dtype=float)
        for j, label in enumerate(fold_model.classes_):
            fold_prob[:, int(label)] = fold_raw[:, j]
        fold_prior = np.bincount(y_train[tr], minlength=3).astype(float) / len(tr)
        fold_prob = (
            direction_probability_blend * fold_prob
            + (1.0 - direction_probability_blend) * fold_prior
        )
        walk_forward.append({"fold": fold, **_direction_metrics(y_train[va], fold_prob)})

    # Model 2: one multi-output model for next-session low/high/close return.
    range_targets = np.column_stack((
        daily_targets["low_return"], daily_targets["high_return"], daily_targets["close_return"]
    ))
    range_model = RandomForestRegressor(
        n_estimators=320, max_depth=5, min_samples_leaf=5,
        max_features=0.8, bootstrap=True, random_state=RANDOM_STATE,
        n_jobs=2,
    )
    range_model.fit(X_train, range_targets[:split])
    range_prediction = range_model.predict(X_test)
    range_baseline = DummyRegressor(strategy="median")
    range_baseline.fit(X_train, range_targets[:split])
    range_base_pred = range_baseline.predict(X_test)
    range_holdout = {
        "low_return_mae": float(mean_absolute_error(range_targets[split:, 0], range_prediction[:, 0])),
        "high_return_mae": float(mean_absolute_error(range_targets[split:, 1], range_prediction[:, 1])),
        "close_return_mae": float(mean_absolute_error(range_targets[split:, 2], range_prediction[:, 2])),
        "combined_mae": float(mean_absolute_error(range_targets[split:], range_prediction)),
        "baseline_combined_mae": float(mean_absolute_error(range_targets[split:], range_base_pred)),
        "close_inside_predicted_range_rate": float(np.mean(
            (range_targets[split:, 2] >= np.minimum(range_prediction[:, 0], range_prediction[:, 1]))
            & (range_targets[split:, 2] <= np.maximum(range_prediction[:, 0], range_prediction[:, 1]))
        )),
    }

    # Models 3 and 4: predict same-contract next-session premium decay for CE/PE.
    seller_models: dict[str, Any] = {}
    seller_metrics: dict[str, Any] = {}
    seller_feature_names: dict[str, list[str]] = {}
    seller_datasets: dict[str, tuple[pd.DataFrame, np.ndarray, list[str], list[str]]] = {}
    for side in ("CE", "PE"):
        print(f"Building {side} same-contract next-session samples...", flush=True)
        seller_X, seller_y, seller_columns, seller_dates = build_seller_samples(
            feature_rows, rolling_samples, side
        )
        seller_datasets[side] = (seller_X, seller_y, seller_columns, seller_dates)
        unique_dates = sorted(set(seller_dates))
        cutoff_index = max(1, int(len(unique_dates) * 0.80))
        cutoff_date = unique_dates[cutoff_index]
        train_mask = np.asarray([d < cutoff_date for d in seller_dates], dtype=bool)
        test_mask = ~train_mask
        if train_mask.sum() < 1000 or test_mask.sum() < 500:
            raise ValueError(f"Insufficient {side} matched-contract samples: train={train_mask.sum()}, test={test_mask.sum()}")
        seller_model = LGBMRegressor(
            objective="regression_l1", n_estimators=130, learning_rate=0.025,
            num_leaves=7, max_depth=3, min_child_samples=60,
            reg_lambda=15.0, reg_alpha=2.0,
            colsample_bytree=0.75, subsample=0.8, subsample_freq=1,
            verbosity=-1, random_state=RANDOM_STATE, n_jobs=2,
        )
        seller_model.fit(seller_X.loc[train_mask], seller_y[train_mask])
        pred = seller_model.predict(seller_X.loc[test_mask])
        seller_baseline = DummyRegressor(strategy="median")
        seller_baseline.fit(seller_X.loc[train_mask], seller_y[train_mask])
        baseline_pred = seller_baseline.predict(seller_X.loc[test_mask])
        seller_metrics[side] = {
            "status": "trained",
            "samples": int(len(seller_X)),
            "train_samples": int(train_mask.sum()),
            "holdout_samples": int(test_mask.sum()),
            "train_end": max(d for d, ok in zip(seller_dates, train_mask) if ok),
            "holdout_start": cutoff_date,
            "holdout_end": max(d for d, ok in zip(seller_dates, test_mask) if ok),
            "holdout_mae": float(mean_absolute_error(seller_y[test_mask], pred)),
            "baseline_mae": float(mean_absolute_error(seller_y[test_mask], baseline_pred)),
            "holdout_r2": float(r2_score(seller_y[test_mask], pred)),
            "seller_premium_decay_positive_rate": float(np.mean(seller_y[test_mask] > 0)),
            "target_definition": "(current_contract_premium - next_session_same_contract_premium) / current_contract_premium; positive indicates premium decay before costs/hedging",
        }
        seller_models[side] = seller_model
        seller_feature_names[side] = seller_columns

    # Refit the four approved model groups on all eligible historical samples.
    print("Refitting four approved model groups on all eligible samples...", flush=True)
    final_direction = LGBMClassifier(
        objective="multiclass", num_class=3, class_weight="balanced",
        n_estimators=120, learning_rate=0.025, num_leaves=5, max_depth=2,
        min_child_samples=25, reg_lambda=15.0, reg_alpha=2.0,
        colsample_bytree=0.7, subsample=0.8, subsample_freq=1,
        verbosity=-1, random_state=RANDOM_STATE, n_jobs=2,
    )
    final_direction.fit(X_daily, y_direction)
    final_direction_prior = np.bincount(y_direction, minlength=3).astype(float) / len(y_direction)
    final_range = RandomForestRegressor(
        n_estimators=320, max_depth=5, min_samples_leaf=5,
        max_features=0.8, bootstrap=True, random_state=RANDOM_STATE,
        n_jobs=2,
    )
    final_range.fit(X_daily, range_targets)
    final_seller_models = {}
    for side in ("CE", "PE"):
        seller_X, seller_y, _, _ = seller_datasets[side]
        final_model = LGBMRegressor(
            objective="regression_l1", n_estimators=130, learning_rate=0.025,
            num_leaves=7, max_depth=3, min_child_samples=60,
            reg_lambda=15.0, reg_alpha=2.0,
            colsample_bytree=0.75, subsample=0.8, subsample_freq=1,
            verbosity=-1, random_state=RANDOM_STATE, n_jobs=2,
        )
        final_model.fit(seller_X, seller_y)
        final_seller_models[side] = final_model

    print("Saving four-model research artifact and validation manifest...", flush=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    model_path = output_dir / "nifty_four_models.joblib"
    bundle = {
        "model_version": "NIFTY-Adaptive-Intelligence-V2-Four-Model",
        "pipeline_version": PIPELINE_VERSION,
        "trained_at_utc": datetime.now(timezone.utc).isoformat(),
        "window_sessions": {"recent": 5, "monthly": 22, "includes_latest_completed_session": True},
        "feature_names": feature_names,
        "direction_model": final_direction,
        "direction_probability_prior": final_direction_prior,
        "direction_probability_blend": direction_probability_blend,
        "range_model": final_range,
        "ce_seller_model": final_seller_models["CE"],
        "pe_seller_model": final_seller_models["PE"],
        "direction_labels": LABELS,
        "range_output_order": ["next_low_return", "next_high_return", "next_close_return"],
        "seller_target": "same-contract next-session premium decay return; positive = seller-side theoretical benefit before costs/hedging",
    }
    joblib.dump(bundle, model_path, compress=3)
    model_hash = hashlib.sha256(model_path.read_bytes()).hexdigest()
    data_hash = hashlib.sha256(
        json.dumps(feature_rows, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()
    direction_beats_baseline = (
        direction_holdout["macro_f1"] > direction_baseline_metrics["macro_f1"]
        and direction_holdout["multiclass_brier"] < direction_baseline_metrics["multiclass_brier"]
    )
    range_beats_baseline = range_holdout["combined_mae"] < range_holdout["baseline_combined_mae"]
    ce_beats_baseline = seller_metrics["CE"]["holdout_mae"] < seller_metrics["CE"]["baseline_mae"]
    pe_beats_baseline = seller_metrics["PE"]["holdout_mae"] < seller_metrics["PE"]["baseline_mae"]
    research_validation_pass = all((direction_beats_baseline, range_beats_baseline, ce_beats_baseline, pe_beats_baseline))
    manifest = {
        "model_version": bundle["model_version"],
        "pipeline_version": PIPELINE_VERSION,
        "training_runtime": {
            "python": platform.python_version(),
            "numpy": np.__version__,
            "pandas": pd.__version__,
            "scikit_learn": sklearn.__version__,
            "lightgbm": lightgbm.__version__,
            "joblib": joblib.__version__,
        },
        "status": "trained_research_candidate",
        "deployment_ready": False,
        "promotion_note": "Four model artifacts were trained. Live inference wiring, shadow/paper validation and production promotion are separate steps; no automatic trading is enabled.",
        "b2_reads": 0,
        "b2_writes": 0,
        "historical_sessions": EXPECTED_SESSIONS,
        "eligible_daily_samples": len(X_daily),
        "model_group_count": 4,
        "model_groups": ["direction", "range", "CE_seller", "PE_seller"],
        "daily_train_samples": int(split),
        "daily_holdout_samples": int(len(X_daily) - split),
        "daily_sample_start": dates[0],
        "daily_sample_end": dates[-1],
        "target_definition": "Each eligible origin session uses only completed-session data through that origin; target is the next trading session.",
        "rolling_windows": {
            "recent": "latest 5 completed trading sessions including the forecast-origin session",
            "monthly": "latest 22 completed trading sessions including the forecast-origin session",
            "lookahead_policy": "No feature uses data later than its forecast-origin session.",
        },
        "rolling_feature_count": len(feature_names),
        "feature_selection": {
            "method": "fixed domain-selected compact feature set",
            "previous_feature_count": 253,
            "selected_feature_count": len(feature_names),
            "includes_ohlc_derived_features": True,
            "same_selector_for_training_inference_and_seller_models": True,
        },
        "direction": {
            "model": "LightGBM multiclass with 50% training-prior probability shrinkage",
            "class_labels": LABELS,
            "class_threshold": DIRECTION_THRESHOLD,
            "holdout": direction_holdout,
            "holdout_class_counts": {LABELS[int(k)]: int(v) for k, v in zip(*np.unique(y_test, return_counts=True))},
            "baseline": direction_baseline_metrics,
            "beats_baseline": direction_beats_baseline,
            "walk_forward": walk_forward,
        },
        "range": {
            "model": "RandomForest multi-output regressor",
            "target_order": ["next_low_return", "next_high_return", "next_close_return"],
            "holdout": range_holdout,
            "beats_baseline": range_beats_baseline,
        },
        "ce_seller": {"model": "LightGBM regression_l1", **seller_metrics["CE"], "beats_baseline": ce_beats_baseline},
        "pe_seller": {"model": "LightGBM regression_l1", **seller_metrics["PE"], "beats_baseline": pe_beats_baseline},
        "research_validation_pass": research_validation_pass,
        "feature_data_coverage": {
            "index_ohlc": repair["coverage"].get("index_ohlc"),
            "participant_oi": repair["coverage"].get("participant_oi"),
            "participant_volume": repair["coverage"].get("participant_volume"),
            "nifty_futures": audit.get("core_market_coverage", {}).get("future_data_available"),
        },
        "dataset_sha256": data_hash,
        "model_sha256": model_hash,
        "model_file": model_path.name,
        "seller_samples": {side: seller_metrics[side]["samples"] for side in ("CE", "PE")},
        "disclaimer": "Research output only. Seller premium decay proxy is not realized P&L and does not prove participant identity. No trade guarantee or automatic execution.",
    }
    (output_dir / "four_model_manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (output_dir / "rolling_feature_schema.json").write_text(json.dumps({
        "window_sessions": bundle["window_sessions"],
        "feature_names": feature_names,
        "feature_generation": "same function for historic training and future daily inference",
        "includes_origin_session": True,
        "lookahead_free": True,
    }, indent=2), encoding="utf-8")
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Train four NIFTY seller-intelligence model groups from the verified 494-session archive.")
    parser.add_argument("--checkpoint", type=Path, default=Path("data/historical_audit/checkpoint.json"))
    parser.add_argument("--audit-report", type=Path, default=Path("data/historical_audit/coverage.json"))
    parser.add_argument("--repair-report", type=Path, default=Path("data/context_repair/context_repair_report.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("models/four_model_artifacts"))
    args = parser.parse_args()
    result = train_four_models(args.checkpoint, args.audit_report, args.repair_report, args.output_dir)
    print(json.dumps(result, indent=2), flush=True)


if __name__ == "__main__":
    main()
