from __future__ import annotations

import argparse
import io
import json
import sys
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import joblib
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.nse_ml_features import FEATURE_SCHEMA_VERSION, extract_nifty_features
from scripts.range_model_bundle import predict_range_bundle
from scripts.train_nifty_four_models import build_latest_inference_features


STATE_SCHEMA_VERSION = 1
MAX_NEXT_SESSION_GAP_DAYS = 4
MAX_SHADOW_RECORDS = 500


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
    for name in required_reports:
        item = results.get(name, {})
        if not isinstance(item, dict) or not item.get("file"):
            raise ValueError(f"Required same-session NSE report is unavailable: {name}")
        if item.get("report_date") != session_text:
            raise ValueError(f"NSE report date mismatch for {name}")

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as outer:
        outer.writestr("manifest.json", json.dumps(manifest))
        for item in results.values():
            if not isinstance(item, dict) or not item.get("file"):
                continue
            filename = item["file"]
            source = reports_dir / filename
            if source.is_file():
                outer.write(source, arcname=filename)
    row = extract_nifty_features(buffer.getvalue(), session_date)
    if row.get("feature_schema_version") != FEATURE_SCHEMA_VERSION:
        raise ValueError("Current NSE feature schema does not match the verified archive schema")
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
    low_price = origin_close * (1.0 + low_return)
    high_price = origin_close * (1.0 + high_return)
    close_price = origin_close * (1.0 + close_return)
    if low_price <= 0 or high_price <= low_price or not low_price <= close_price <= high_price:
        raise ValueError("Range forecast failed price-bound sanity checks")
    return {
        "status": "pending",
        "origin_date": origin_date,
        "origin_close": float(origin_close),
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

    actual_low = float(actual_features["spot_low"])
    actual_high = float(actual_features["spot_high"])
    actual_close = float(actual_features["spot_close"])
    if not (0 < actual_low <= actual_close <= actual_high):
        raise ValueError("Actual NIFTY OHLC values failed range validation")

    origin_close = float(forecast["origin_close"])
    actual_low_return = actual_low / origin_close - 1.0
    actual_high_return = actual_high / origin_close - 1.0
    actual_close_return = actual_close / origin_close - 1.0
    predicted_low_return = float(forecast["predicted_low_return"])
    predicted_high_return = float(forecast["predicted_high_return"])
    alpha = float(forecast.get("calibration_alpha", 0.20))
    lower_miss = max(predicted_low_return - actual_low_return, 0.0)
    upper_miss = max(actual_high_return - predicted_high_return, 0.0)
    interval_width = predicted_high_return - predicted_low_return
    winkler = interval_width + (2.0 / alpha) * (lower_miss + upper_miss)

    return {
        **base,
        "status": "scored",
        "actual_low": actual_low,
        "actual_high": actual_high,
        "actual_close": actual_close,
        "range_covered": bool(
            actual_low_return >= predicted_low_return
            and actual_high_return <= predicted_high_return
        ),
        "close_inside_predicted_range": bool(
            actual_close_return >= predicted_low_return
            and actual_close_return <= predicted_high_return
        ),
        "low_absolute_error_points": abs(float(forecast["predicted_low"]) - actual_low),
        "high_absolute_error_points": abs(float(forecast["predicted_high"]) - actual_high),
        "close_absolute_error_points": abs(float(forecast["predicted_close"]) - actual_close),
        "winkler_interval_score_return_space": float(winkler),
        "evaluation_note": "First subsequent aligned EOD session within the four-calendar-day safety window; no trading P&L is inferred.",
    }


def advance_shadow_state(
    state: dict[str, Any],
    current_row: dict[str, Any],
    inference_features: dict[str, float],
    bundle: dict[str, Any],
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
                "scored_records": sum(item.get("status") == "scored" for item in records),
            }
        scored_or_skipped = score_range_forecast(pending, current_date, current_row["features"])
        records.append(scored_or_skipped)
        records = records[-MAX_SHADOW_RECORDS:]

    forecast = make_range_forecast(bundle, current_date, current_close, inference_features)
    new_state = {
        "schema_version": STATE_SCHEMA_VERSION,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "pending_forecast": forecast,
        "records": records,
        "evaluation_policy": {
            "target": "next NIFTY trading session",
            "observed_session_rule": "first subsequent aligned NSE EOD session within four calendar days",
            "stale_forecasts": "skipped, never scored against a later multi-session gap",
            "b2_reads": 0,
            "b2_writes": 0,
        },
    }
    scored = [item for item in records if item.get("status") == "scored"]
    summary = {
        "status": "forecast_created",
        "origin_date": current_date,
        "pending_forecast": forecast,
        "newly_evaluated_record": scored_or_skipped,
        "scored_records": len(scored),
        "range_coverage_rate": (
            float(np.mean([item["range_covered"] for item in scored])) if scored else None
        ),
        "close_inside_rate": (
            float(np.mean([item["close_inside_predicted_range"] for item in scored])) if scored else None
        ),
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

    bundle = joblib.load(bundle_path)
    if bundle.get("schema_version") != 1:
        raise RuntimeError("Unsupported Range candidate bundle")
    state = (
        json.loads(state_path.read_text(encoding="utf-8"))
        if state_path.is_file()
        else {"schema_version": STATE_SCHEMA_VERSION, "records": [], "pending_forecast": None}
    )
    if state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise RuntimeError("Unsupported Range shadow-state schema")

    next_state, summary = advance_shadow_state(state, current_row, inference_features, bundle)
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
