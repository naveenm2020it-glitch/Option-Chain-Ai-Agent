from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.nse_ml_features import FEATURE_SCHEMA_VERSION, extract_nifty_features
from scripts.range_model_bundle import predict_range_bundle
from scripts.range_research_benchmark import Q_HIGH, Q_LOW
from scripts.train_nifty_four_models import build_latest_inference_features


STATE_SCHEMA_VERSION = 1
MAX_NEXT_SESSION_GAP_DAYS = 4
MAX_SHADOW_RECORDS = 500
SHADOW_MIN_SESSIONS = 20
SHADOW_MIN_RANGE_COVERAGE = 0.85
SHADOW_MIN_BASELINE_WIN_RATE = 0.55
SHADOW_MAX_CLOSE_MAE_RATIO = 1.10


def extract_current_feature_row(reports_dir: Path) -> dict[str, Any]:
    manifest_path = reports_dir / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("Current NSE report manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("core_session_status") != "aligned":
        raise ValueError("NSE core reports are not aligned to one trading session")
    session_text = manifest.get("core_session_date")
    if not isinstance(session_text, str):
        raise ValueError("NSE manifest does not identify an aligned session date")
    session_date = date.fromisoformat(session_text)

    required_reports = (
        "fo_udiff_bhavcopy",
        "fo_participant_oi",
        "fo_participant_volume",
        "nifty_index_close_all",
    )
    results = manifest.get("results", {})
    required_files = []
    for name in required_reports:
        item = results.get(name, {})
        if not isinstance(item, dict) or not item.get("file"):
            raise ValueError(f"Required same-session NSE report is unavailable: {name}")
        if item.get("report_date") != session_text:
            raise ValueError(f"NSE report date mismatch for {name}")
        filename = str(item["file"])
        if Path(filename).name != filename:
            raise ValueError(f"Unsafe NSE report filename for {name}")
        source = reports_dir / filename
        if not source.is_file():
            raise ValueError(f"Required NSE report file is missing on disk: {name}")
        required_files.append(filename)

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as outer:
        outer.writestr("manifest.json", json.dumps(manifest))
        seen_files = set()
        for item in results.values():
            if not isinstance(item, dict) or not item.get("file"):
                continue
            filename = str(item["file"])
            if Path(filename).name != filename or filename in seen_files:
                continue
            source = reports_dir / filename
            if source.is_file():
                outer.write(source, arcname=filename)
                seen_files.add(filename)
    if not set(required_files).issubset(seen_files):
        raise ValueError("One or more required NSE report files could not be packaged")

    row = extract_nifty_features(buffer.getvalue(), session_date)
    if row.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        raise ValueError("Current NSE feature schema does not match the verified archive schema")

    features = row.get("features", {})
    quality_flags = (
        "index_data_available",
        "participant_oi_data_available",
        "participant_volume_data_available",
        "future_data_available",
    )
    for flag in quality_flags:
        try:
            available = float(features.get(flag, 0.0))
        except (TypeError, ValueError):
            available = 0.0
        if available < 1.0:
            raise ValueError(f"Current NSE session failed required feature coverage gate: {flag}")

    spot_open = float(features.get("spot_open", 0.0))
    spot_high = float(features.get("spot_high", 0.0))
    spot_low = float(features.get("spot_low", 0.0))
    spot_close = float(features.get("spot_close", 0.0))
    if not (0 < spot_low <= spot_open <= spot_high and spot_low <= spot_close <= spot_high):
        raise ValueError("Current NIFTY index OHLC values failed integrity validation")
    return row


def make_range_forecast(
    bundle: dict[str, Any],
    origin_date: str,
    origin_close: float,
    features: dict[str, float],
) -> dict[str, Any]:
    if origin_close <= 0 or not np.isfinite(origin_close):
        raise ValueError("Forecast origin close must be positive and finite")
    returns = predict_range_bundle(bundle, features)
    low_return = returns["low_return"]
    high_return = returns["high_return"]
    close_return = returns["close_return"]
    core_low_return = float(returns.get("core_low_return", low_return))
    core_high_return = float(returns.get("core_high_return", high_return))
    core_low_price = origin_close * (1.0 + core_low_return)
    core_high_price = origin_close * (1.0 + core_high_return)
    low_price = origin_close * (1.0 + low_return)
    high_price = origin_close * (1.0 + high_return)
    close_price = origin_close * (1.0 + close_return)
    if low_price <= 0 or high_price <= low_price or not low_price <= close_price <= high_price:
        raise ValueError("Range forecast failed price-bound sanity checks")
    return {
        "status": "pending",
        "origin_date": origin_date,
        "origin_close": float(origin_close),
        "core_low_return": core_low_return,
        "core_high_return": core_high_return,
        "core_close_return": float(returns.get("core_close_return", close_return)),
        "core_predicted_low": float(core_low_price),
        "core_predicted_high": float(core_high_price),
        "predicted_low_return": float(low_return),
        "predicted_high_return": float(high_return),
        "predicted_close_return": float(close_return),
        "predicted_low": float(low_price),
        "predicted_high": float(high_price),
        "predicted_close": float(close_price),
        "candidate_name": bundle["candidate"].get("name", "unnamed_range_candidate"),
        "calibration_alpha": float(bundle.get("calibration_alpha", 0.20)),
        "forecast_created_at_utc": datetime.now(timezone.utc).isoformat(),
        "target_rule": "first subsequent aligned NSE EOD session; target date is not assumed in advance",
    }


def _interval_metrics(
    predicted_low_return: float,
    predicted_high_return: float,
    predicted_close_return: float,
    actual_low_return: float,
    actual_high_return: float,
    actual_close_return: float,
    origin_close: float,
    alpha: float,
) -> dict[str, Any]:
    if not 0.0 < alpha < 1.0:
        raise ValueError("Conformal alpha must be between zero and one")
    if predicted_low_return >= predicted_high_return:
        raise ValueError("Predicted Range interval is invalid")

    low_covered = actual_low_return >= predicted_low_return
    high_covered = actual_high_return <= predicted_high_return
    close_inside = predicted_low_return <= actual_close_return <= predicted_high_return
    lower_miss = max(predicted_low_return - actual_low_return, 0.0)
    upper_miss = max(actual_high_return - predicted_high_return, 0.0)
    interval_width = predicted_high_return - predicted_low_return
    winkler = interval_width + (2.0 / alpha) * (lower_miss + upper_miss)

    return {
        "range_covered": bool(low_covered and high_covered),
        "low_endpoint_covered": bool(low_covered),
        "high_endpoint_covered": bool(high_covered),
        "close_inside_predicted_range": bool(close_inside),
        "winkler_interval_score_return_space": float(winkler),
        "interval_width_return": float(interval_width),
        "predicted_range_width_points": float(interval_width * origin_close),
        "actual_range_width_points": float((actual_high_return - actual_low_return) * origin_close),
        "range_width_difference_points": float((interval_width - (actual_high_return - actual_low_return)) * origin_close),
        "range_width_ratio": float(interval_width / max(actual_high_return - actual_low_return, 1e-12)),
        "low_signed_error_points": float((predicted_low_return - actual_low_return) * origin_close),
        "high_signed_error_points": float((predicted_high_return - actual_high_return) * origin_close),
        "low_absolute_error_points": abs(predicted_low_return - actual_low_return) * origin_close,
        "high_absolute_error_points": abs(predicted_high_return - actual_high_return) * origin_close,
        "close_absolute_error_points": abs(predicted_close_return - actual_close_return) * origin_close,
    }


def make_recent_22_session_baseline(
    feature_rows: dict[str, dict[str, Any]],
    origin_date: str,
    origin_close: float,
) -> dict[str, Any]:
    """Leakage-safe empirical 10th/90th range quantiles from the prior 22 completed sessions."""
    dates = sorted(feature_rows)
    if origin_date not in feature_rows:
        raise ValueError("Range baseline origin date is absent from feature history")
    origin_index = dates.index(origin_date)
    first_target_index = max(1, origin_index - 21)
    if origin_index - first_target_index + 1 < 22:
        raise ValueError("Range baseline needs 22 completed historical session ranges")
    if origin_close <= 0 or not np.isfinite(origin_close):
        raise ValueError("Range baseline origin close must be positive and finite")

    low_returns, high_returns, close_returns = [], [], []
    for target_index in range(first_target_index, origin_index + 1):
        prior = feature_rows[dates[target_index - 1]].get("features", {})
        actual = feature_rows[dates[target_index]].get("features", {})
        prior_close = float(prior.get("spot_close", 0.0))
        low = float(actual.get("spot_low", 0.0))
        high = float(actual.get("spot_high", 0.0))
        close = float(actual.get("spot_close", 0.0))
        if not (prior_close > 0 and 0 < low <= close <= high):
            raise ValueError("Historical OHLC integrity failed while building the Range baseline")
        low_returns.append(low / prior_close - 1.0)
        high_returns.append(high / prior_close - 1.0)
        close_returns.append(close / prior_close - 1.0)

    low_return = float(np.quantile(np.asarray(low_returns), Q_LOW))
    high_return = float(np.quantile(np.asarray(high_returns), Q_HIGH))
    close_return = float(np.median(np.asarray(close_returns)))
    low_return = min(low_return, close_return)
    high_return = max(high_return, close_return)
    predicted_low = origin_close * (1.0 + low_return)
    predicted_high = origin_close * (1.0 + high_return)
    predicted_close = origin_close * (1.0 + close_return)
    if not (0 < predicted_low <= predicted_close <= predicted_high):
        raise ValueError("Rolling 22-session Range baseline failed price-bound checks")
    return {
        "name": "rolling_22_session_empirical_quantile",
        "lookback_sessions": 22,
        "predicted_low_return": low_return,
        "predicted_high_return": high_return,
        "predicted_close_return": close_return,
        "predicted_low": float(predicted_low),
        "predicted_high": float(predicted_high),
        "predicted_close": float(predicted_close),
    }


def score_range_forecast(
    forecast: dict[str, Any],
    actual_session_date: str,
    actual_features: dict[str, Any],
) -> dict[str, Any]:
    origin_date = date.fromisoformat(forecast["origin_date"])
    actual_date = date.fromisoformat(actual_session_date)
    elapsed = (actual_date - origin_date).days
    if elapsed <= 0:
        raise ValueError("Actual session must be after the forecast origin")

    base = {
        "forecast_origin_date": forecast["origin_date"],
        "actual_session_date": actual_session_date,
        "elapsed_calendar_days": elapsed,
        "candidate_name": forecast.get("candidate_name"),
        "predicted_low": float(forecast["predicted_low"]),
        "predicted_high": float(forecast["predicted_high"]),
        "predicted_close": float(forecast["predicted_close"]),
    }
    if elapsed > MAX_NEXT_SESSION_GAP_DAYS:
        return {
            **base,
            "status": "skipped_stale_gap",
            "evaluation_note": (
                "More than four calendar days elapsed; the system will not assume "
                "the observed session was the immediate next trading session."
            ),
        }

    # Conservative continuity guard: only score when exactly one Monday-Friday
    # date elapsed. This intentionally skips some exchange-holiday gaps rather
    # than falsely scoring a second or later trading session as the next session.
    weekday_steps = int(np.busday_count(
        (origin_date + timedelta(days=1)).isoformat(),
        (actual_date + timedelta(days=1)).isoformat(),
    ))
    if weekday_steps != 1:
        return {
            **base,
            "status": "skipped_nonadjacent_session",
            "weekday_steps": weekday_steps,
            "evaluation_note": (
                "The observed EOD date is not the first weekday after the forecast origin. "
                "A session may have been missed or an exchange holiday may be present; "
                "the forecast is skipped to avoid false next-session validation."
            ),
        }

    actual_low = float(actual_features["spot_low"])
    actual_high = float(actual_features["spot_high"])
    actual_close = float(actual_features["spot_close"])
    if not (0 < actual_low <= actual_close <= actual_high):
        raise ValueError("Actual NIFTY OHLC values failed range validation")

    origin_close = float(forecast["origin_close"])
    actual_low_return = actual_low / origin_close - 1.0
    actual_high_return = actual_high / origin_close - 1.0
    actual_close_return = actual_close / origin_close - 1.0
    alpha = float(forecast.get("calibration_alpha", 0.10))
    predicted_close_return = float(
        forecast.get("predicted_close_return", float(forecast["predicted_close"]) / origin_close - 1.0)
    )
    model_metrics = _interval_metrics(
        float(forecast["predicted_low_return"]),
        float(forecast["predicted_high_return"]),
        predicted_close_return,
        actual_low_return,
        actual_high_return,
        actual_close_return,
        origin_close,
        alpha,
    )

    result = {
        **base,
        "status": "scored",
        "actual_low": actual_low,
        "actual_high": actual_high,
        "actual_close": actual_close,
        "range_covered": model_metrics["range_covered"],
        "low_endpoint_covered": model_metrics["low_endpoint_covered"],
        "high_endpoint_covered": model_metrics["high_endpoint_covered"],
        "close_inside_predicted_range": model_metrics["close_inside_predicted_range"],
        "low_absolute_error_points": model_metrics["low_absolute_error_points"],
        "high_absolute_error_points": model_metrics["high_absolute_error_points"],
        "close_absolute_error_points": model_metrics["close_absolute_error_points"],
        "low_signed_error_points": model_metrics["low_signed_error_points"],
        "high_signed_error_points": model_metrics["high_signed_error_points"],
        "predicted_range_width_points": model_metrics["predicted_range_width_points"],
        "actual_range_width_points": model_metrics["actual_range_width_points"],
        "range_width_difference_points": model_metrics["range_width_difference_points"],
        "range_width_ratio": model_metrics["range_width_ratio"],
        "winkler_interval_score_return_space": model_metrics["winkler_interval_score_return_space"],
        "mean_interval_width_return": model_metrics["interval_width_return"],
        "calibration_alpha": alpha,
        "evaluation_note": "First subsequent aligned EOD session within the four-calendar-day safety window; no trading P&L is inferred.",
    }

    baseline = forecast.get("baseline")
    if isinstance(baseline, dict):
        baseline_metrics = _interval_metrics(
            float(baseline["predicted_low_return"]),
            float(baseline["predicted_high_return"]),
            float(baseline["predicted_close_return"]),
            actual_low_return,
            actual_high_return,
            actual_close_return,
            origin_close,
            alpha,
        )
        result.update({
            "baseline_name": baseline.get("name", "rolling_22_session_empirical_quantile"),
            "baseline_range_covered": baseline_metrics["range_covered"],
            "baseline_low_endpoint_covered": baseline_metrics["low_endpoint_covered"],
            "baseline_high_endpoint_covered": baseline_metrics["high_endpoint_covered"],
            "baseline_close_inside_predicted_range": baseline_metrics["close_inside_predicted_range"],
            "baseline_low_absolute_error_points": baseline_metrics["low_absolute_error_points"],
            "baseline_high_absolute_error_points": baseline_metrics["high_absolute_error_points"],
            "baseline_close_absolute_error_points": baseline_metrics["close_absolute_error_points"],
            "baseline_predicted_range_width_points": baseline_metrics["predicted_range_width_points"],
            "baseline_actual_range_width_points": baseline_metrics["actual_range_width_points"],
            "baseline_range_width_difference_points": baseline_metrics["range_width_difference_points"],
            "baseline_range_width_ratio": baseline_metrics["range_width_ratio"],
            "baseline_winkler_interval_score_return_space": baseline_metrics["winkler_interval_score_return_space"],
            "baseline_interval_width_return": baseline_metrics["interval_width_return"],
            "model_beats_baseline_winkler": (
                model_metrics["winkler_interval_score_return_space"]
                < baseline_metrics["winkler_interval_score_return_space"]
            ),
            "model_beats_baseline_close_mae": (
                model_metrics["close_absolute_error_points"]
                <= baseline_metrics["close_absolute_error_points"]
            ),
        })
    return result


def summarize_shadow_records(records: list[dict[str, Any]]) -> dict[str, Any]:
    scored = [item for item in records if item.get("status") == "scored"]
    comparable = [
        item for item in scored
        if "baseline_winkler_interval_score_return_space" in item
    ]
    summary: dict[str, Any] = {
        "scored_sessions": len(scored),
        "skipped_nonadjacent_session_count": sum(
            item.get("status") == "skipped_nonadjacent_session" for item in records
        ),
        "skipped_stale_gap_count": sum(
            item.get("status") == "skipped_stale_gap" for item in records
        ),
        "minimum_sessions_required": SHADOW_MIN_SESSIONS,
        "nominal_full_range_coverage": (
            float(np.mean([1.0 - float(item.get("calibration_alpha", 0.10)) for item in scored]))
            if scored else 0.90
        ),
        "minimum_acceptable_shadow_coverage": SHADOW_MIN_RANGE_COVERAGE,
        "shadow_validation_status": "collecting",
        "production_promotion_allowed": False,
        "manual_promotion_review_required": True,
    }
    if not scored:
        summary.update({
            "range_coverage_rate": None,
            "low_endpoint_coverage_rate": None,
            "high_endpoint_coverage_rate": None,
            "close_inside_rate": None,
            "baseline_range_coverage_rate": None,
            "baseline_low_endpoint_coverage_rate": None,
            "baseline_high_endpoint_coverage_rate": None,
            "baseline_close_inside_rate": None,
            "mean_model_winkler": None,
            "mean_baseline_winkler": None,
            "model_winkler_win_rate": None,
            "mean_model_close_mae_points": None,
            "mean_baseline_close_mae_points": None,
            "mean_model_high_mae_points": None,
            "mean_model_low_mae_points": None,
            "mean_model_predicted_width_points": None,
            "mean_actual_width_points": None,
            "mean_model_width_difference_points": None,
            "mean_model_width_ratio": None,
            "mean_baseline_predicted_width_points": None,
            "mean_baseline_width_difference_points": None,
            "mean_baseline_width_ratio": None,
        })
        return summary

    summary.update({
        "range_coverage_rate": float(np.mean([item["range_covered"] for item in scored])),
        "low_endpoint_coverage_rate": float(np.mean([item["low_endpoint_covered"] for item in scored])),
        "high_endpoint_coverage_rate": float(np.mean([item["high_endpoint_covered"] for item in scored])),
        "close_inside_rate": float(np.mean([item["close_inside_predicted_range"] for item in scored])),
        "mean_model_winkler": float(np.mean([
            item["winkler_interval_score_return_space"] for item in scored
        ])),
        "mean_model_close_mae_points": float(np.mean([
            item["close_absolute_error_points"] for item in scored
        ])),
        "mean_model_high_mae_points": float(np.mean([item["high_absolute_error_points"] for item in scored])),
        "mean_model_low_mae_points": float(np.mean([item["low_absolute_error_points"] for item in scored])),
        "mean_model_predicted_width_points": float(np.mean([item["predicted_range_width_points"] for item in scored])),
        "mean_actual_width_points": float(np.mean([item["actual_range_width_points"] for item in scored])),
        "mean_model_width_difference_points": float(np.mean([item["range_width_difference_points"] for item in scored])),
        "mean_model_width_ratio": float(np.mean([item["range_width_ratio"] for item in scored])),
    })
    if comparable:
        summary.update({
            "baseline_range_coverage_rate": float(np.mean([
                item["baseline_range_covered"] for item in comparable
            ])),
            "baseline_low_endpoint_coverage_rate": float(np.mean([
                item["baseline_low_endpoint_covered"] for item in comparable
            ])),
            "baseline_high_endpoint_coverage_rate": float(np.mean([
                item["baseline_high_endpoint_covered"] for item in comparable
            ])),
            "baseline_close_inside_rate": float(np.mean([
                item["baseline_close_inside_predicted_range"] for item in comparable
            ])),
            "mean_baseline_predicted_width_points": float(np.mean([item["baseline_predicted_range_width_points"] for item in comparable])),
            "mean_baseline_width_difference_points": float(np.mean([item["baseline_range_width_difference_points"] for item in comparable])),
            "mean_baseline_width_ratio": float(np.mean([item["baseline_range_width_ratio"] for item in comparable])),
        })
    else:
        summary.update({
            "baseline_range_coverage_rate": None,
            "baseline_low_endpoint_coverage_rate": None,
            "baseline_high_endpoint_coverage_rate": None,
            "baseline_close_inside_rate": None,
            "mean_baseline_predicted_width_points": None,
            "mean_baseline_width_difference_points": None,
            "mean_baseline_width_ratio": None,
        })
    if comparable:
        model_winkler = np.asarray([
            item["winkler_interval_score_return_space"] for item in comparable
        ], dtype=float)
        baseline_winkler = np.asarray([
            item["baseline_winkler_interval_score_return_space"] for item in comparable
        ], dtype=float)
        model_close_mae = float(np.mean([
            item["close_absolute_error_points"] for item in comparable
        ]))
        baseline_close_mae = float(np.mean([
            item["baseline_close_absolute_error_points"] for item in comparable
        ]))
        win_rate = float(np.mean(model_winkler < baseline_winkler))
        mean_model_winkler = float(np.mean(model_winkler))
        mean_baseline_winkler = float(np.mean(baseline_winkler))
        summary.update({
            "mean_model_winkler": mean_model_winkler,
            "mean_baseline_winkler": mean_baseline_winkler,
            "model_winkler_win_rate": win_rate,
            "mean_model_close_mae_points": model_close_mae,
            "mean_baseline_close_mae_points": baseline_close_mae,
        })
        if len(comparable) >= SHADOW_MIN_SESSIONS:
            passed = (
                summary["range_coverage_rate"] >= SHADOW_MIN_RANGE_COVERAGE
                and mean_model_winkler < mean_baseline_winkler
                and win_rate >= SHADOW_MIN_BASELINE_WIN_RATE
                and model_close_mae <= max(baseline_close_mae, 1e-9) * SHADOW_MAX_CLOSE_MAE_RATIO
            )
            summary["shadow_validation_status"] = "pass_manual_review" if passed else "fail"
    return summary


def advance_shadow_state(
    state: dict[str, Any],
    current_row: dict[str, Any],
    inference_features: dict[str, float],
    bundle: dict[str, Any],
    baseline_forecast: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    current_date = str(current_row["session_date"])
    current_close = float(current_row["features"]["spot_close"])
    records = list(state.get("records", []))
    pending = state.get("pending_forecast")
    scored_or_skipped = None

    if pending:
        pending_date = date.fromisoformat(pending["origin_date"])
        observed_date = date.fromisoformat(current_date)
        if observed_date < pending_date:
            raise ValueError("Current NSE session is older than the pending forecast origin")
        if observed_date == pending_date:
            # Re-running the collector for the same session must not create
            # duplicate forecasts or score a forecast against its own origin.
            return state, {
                "status": "same_session_idempotent",
                "origin_date": current_date,
                **summarize_shadow_records(records),
                "b2_reads": 0,
                "b2_writes": 0,
                "deployment_ready": False,
            }
        scored_or_skipped = score_range_forecast(pending, current_date, current_row["features"])
        records.append(scored_or_skipped)
        records = records[-MAX_SHADOW_RECORDS:]

    forecast = make_range_forecast(bundle, current_date, current_close, inference_features)
    if baseline_forecast is not None:
        forecast["baseline"] = baseline_forecast
    new_state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pending_forecast": forecast,
        "records": records,
        "evaluation_policy": {
            "target": "next NIFTY trading session",
            "observed_session_rule": "score only when exactly one Monday-Friday date follows the forecast origin; otherwise skip conservatively",
            "stale_forecasts": "skipped; non-adjacent weekday gaps and possible exchange-holiday gaps are also skipped",
            "b2_reads": 0,
            "b2_writes": 0,
        },
    }
    summary = {
        "status": "forecast_created",
        "origin_date": current_date,
        "pending_forecast": forecast,
        "newly_evaluated_record": scored_or_skipped,
        **summarize_shadow_records(records),
        "b2_reads": 0,
        "b2_writes": 0,
        "deployment_ready": False,
    }
    return new_state, summary


def run_shadow(
    checkpoint_path: Path,
    reports_dir: Path,
    bundle_path: Path,
    state_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    if not checkpoint_path.is_file() or not bundle_path.is_file():
        raise RuntimeError("Range shadow requires the verified archive checkpoint and candidate bundle")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    feature_rows = checkpoint.get("features", {})
    if len(feature_rows) != 494:
        raise RuntimeError(f"Range shadow expected 494 verified historical sessions; found {len(feature_rows)}")
    latest_archive_date = max(feature_rows)
    if any(row.get("feature_schema_version") != FEATURE_SCHEMA_VERSION for row in feature_rows.values()):
        raise RuntimeError("Historical feature schema is inconsistent; shadow forecast is blocked")

    current_row = extract_current_feature_row(reports_dir)
    current_date = current_row["session_date"]
    if current_date <= latest_archive_date:
        raise RuntimeError(
            f"Current session {current_date} must be newer than the frozen archive end {latest_archive_date}"
        )

    merged_features = dict(feature_rows)
    merged_features[current_date] = current_row
    inference_features = build_latest_inference_features(merged_features)

    manifest_path = bundle_path.parent / "range_candidate_manifest.json"
    if not manifest_path.is_file():
        raise RuntimeError("Range candidate manifest is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("status") != "range_research_candidate_built":
        raise RuntimeError("Range candidate manifest status is invalid")
    if manifest.get("forward_shadow_validation_required") is not True:
        raise RuntimeError("Range candidate manifest does not require forward shadow validation")
    if manifest.get("deployment_ready") is not False or manifest.get("production_promotion_allowed") is not False:
        raise RuntimeError("Range candidate manifest violates the production-promotion lock")
    if manifest.get("archive_sessions") != 494 or manifest.get("archive_audit_pass") is not True:
        raise RuntimeError("Range candidate manifest does not match the verified archive gates")
    if hashlib.sha256(bundle_path.read_bytes()).hexdigest() != manifest.get("model_sha256"):
        raise RuntimeError("Range candidate SHA256 does not match its manifest")

    bundle = joblib.load(bundle_path)
    if bundle.get("schema_version") not in (1, 2):
        raise RuntimeError("Unsupported Range candidate bundle")
    if abs(float(bundle.get("calibration_alpha", -1.0)) - float(manifest.get("calibration_alpha", -2.0))) > 1e-12:
        raise RuntimeError("Range candidate calibration metadata does not match its manifest")
    if not 0.0 < float(bundle.get("calibration_alpha", 0.0)) < 1.0:
        raise RuntimeError("Range candidate has an invalid conformal calibration alpha")
    if manifest.get("context_repair_pass") is not True:
        raise RuntimeError("Range candidate manifest does not pass context repair")
    if manifest.get("feature_count") != len(bundle.get("feature_names", [])):
        raise RuntimeError("Range candidate feature count does not match its manifest")
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.is_file()
        else {"schema_version": STATE_SCHEMA_VERSION, "records": [], "pending_forecast": None}
    )
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise RuntimeError("Unsupported Range shadow-state schema")

    baseline_forecast = make_recent_22_session_baseline(
        merged_features, current_date, float(current_row["features"]["spot_close"])
    )
    next_state, summary = advance_shadow_state(
        state, current_row, inference_features, bundle, baseline_forecast
    )
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(next_state, indent=2, sort_keys=True), encoding="utf-8")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Create and score leakage-safe NIFTY Range shadow forecasts.")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--reports-dir", type=Path, required=True)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    try:
        report = run_shadow(
            args.checkpoint, args.reports_dir, args.bundle, args.state, args.output
        )
    except Exception as exc:
        # The shadow monitor is deliberately fail-closed and non-blocking for
        # the independent NSE archive writer. It keeps prior state untouched.
        report = {
            "status": "blocked",
            "reason": f"{type(exc).__name__}: {str(exc)[:500]}",
            "b2_reads": 0,
            "b2_writes": 0,
            "deployment_ready": False,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
