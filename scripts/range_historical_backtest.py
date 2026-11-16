from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.range_model_bundle import fit_range_bundle, predict_range_bundle
from scripts.range_shadow_validation import (
    _interval_metrics,
    make_recent_22_session_baseline,
    summarize_shadow_records,
)
from scripts.train_nifty_four_models import (
    EXPECTED_SESSIONS,
    _build_daily_targets,
    _feature_matrix,
    build_rolling_feature_rows,
)


def _score_pair(
    forecast: dict[str, Any],
    baseline: dict[str, Any],
    target_date: str,
    actual: dict[str, Any],
) -> dict[str, Any]:
    origin_close = float(forecast["origin_close"])
    actual_low = float(actual["spot_low"])
    actual_high = float(actual["spot_high"])
    actual_close = float(actual["spot_close"])
    if not (0 < actual_low <= actual_close <= actual_high):
        raise ValueError(f"{target_date}: actual NIFTY OHLC ordering is invalid")

    actual_low_return = actual_low / origin_close - 1.0
    actual_high_return = actual_high / origin_close - 1.0
    actual_close_return = actual_close / origin_close - 1.0
    alpha = float(forecast["calibration_alpha"])

    model = _interval_metrics(
        float(forecast["predicted_low_return"]),
        float(forecast["predicted_high_return"]),
        float(forecast["predicted_close_return"]),
        actual_low_return,
        actual_high_return,
        actual_close_return,
        origin_close,
        alpha,
    )
    base = _interval_metrics(
        float(baseline["predicted_low_return"]),
        float(baseline["predicted_high_return"]),
        float(baseline["predicted_close_return"]),
        actual_low_return,
        actual_high_return,
        actual_close_return,
        origin_close,
        alpha,
    )
    return {
        "status": "scored",
        "forecast_origin_date": forecast["origin_date"],
        "actual_session_date": target_date,
        "candidate_name": forecast["candidate_name"],
        "predicted_low": float(forecast["predicted_low"]),
        "predicted_high": float(forecast["predicted_high"]),
        "predicted_close": float(forecast["predicted_close"]),
        "actual_low": actual_low,
        "actual_high": actual_high,
        "actual_close": actual_close,
        "calibration_alpha": alpha,
        "range_covered": model["range_covered"],
        "low_endpoint_covered": model["low_endpoint_covered"],
        "high_endpoint_covered": model["high_endpoint_covered"],
        "close_inside_predicted_range": model["close_inside_predicted_range"],
        "winkler_interval_score_return_space": model["winkler_interval_score_return_space"],
        "low_absolute_error_points": model["low_absolute_error_points"],
        "high_absolute_error_points": model["high_absolute_error_points"],
        "close_absolute_error_points": model["close_absolute_error_points"],
        "predicted_range_width_points": model["predicted_range_width_points"],
        "actual_range_width_points": model["actual_range_width_points"],
        "range_width_difference_points": model["range_width_difference_points"],
        "range_width_ratio": model["range_width_ratio"],
        "baseline_name": baseline["name"],
        "baseline_range_covered": base["range_covered"],
        "baseline_low_endpoint_covered": base["low_endpoint_covered"],
        "baseline_high_endpoint_covered": base["high_endpoint_covered"],
        "baseline_close_inside_predicted_range": base["close_inside_predicted_range"],
        "baseline_winkler_interval_score_return_space": base["winkler_interval_score_return_space"],
        "baseline_close_absolute_error_points": base["close_absolute_error_points"],
        "baseline_predicted_range_width_points": base["predicted_range_width_points"],
        "baseline_range_width_difference_points": base["range_width_difference_points"],
        "baseline_range_width_ratio": base["range_width_ratio"],
        "model_beats_baseline_winkler": (
            model["winkler_interval_score_return_space"]
            < base["winkler_interval_score_return_space"]
        ),
        "model_beats_baseline_close_mae": (
            model["close_absolute_error_points"] <= base["close_absolute_error_points"]
        ),
    }


def run_historical_walk_forward(
    checkpoint_path: Path, tuning_report_path: Path, output_path: Path, sessions: int = 40
) -> dict[str, Any]:
    if sessions != 40:
        raise ValueError("This validation is deliberately locked to exactly 40 next-session outcomes")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    feature_rows = checkpoint.get("features", {})
    if len(feature_rows) != EXPECTED_SESSIONS:
        raise RuntimeError(
            f"Expected {EXPECTED_SESSIONS} verified archive sessions; found {len(feature_rows)}"
        )

    tuning_report = json.loads(tuning_report_path.read_text(encoding="utf-8"))
    range_report = tuning_report.get("range", {})
    if range_report.get("cv_beats_baseline") is not True:
        raise RuntimeError("Locked Range candidate did not pass its chronological CV gate")
    if float(range_report.get("selected_calibration_alpha", -1.0)) != 0.10:
        raise RuntimeError("Unexpected Range conformal calibration alpha")
    candidate = range_report.get("selected_config")
    if not isinstance(candidate, dict) or not candidate.get("name"):
        raise RuntimeError("Tuning report does not contain a locked selected Range candidate")

    samples = build_rolling_feature_rows(feature_rows)
    if len(samples) < sessions + 150:
        raise RuntimeError("Insufficient chronological samples for 40-session walk-forward validation")
    targets = _build_daily_targets(samples, feature_rows)
    target_matrix = np.column_stack(
        (targets["low_return"], targets["high_return"], targets["close_return"])
    )
    all_X, _ = _feature_matrix(samples)

    # The final 40 target sessions are held out as forecast outcomes. For each
    # origin, training rows are restricted to samples whose target_date is no
    # later than that origin. Thus no target-day or future data enters fitting.
    test_samples = samples[-sessions:]
    records: list[dict[str, Any]] = []
    fit_counts: list[int] = []
    for sample in test_samples:
        origin_date = str(sample["origin_date"])
        target_date = str(sample["target_date"])
        origin_index = int(sample["origin_index"])
        train_indices = [
            index for index, earlier in enumerate(samples)
            if int(earlier["origin_index"]) < origin_index
            and str(earlier["target_date"]) <= origin_date
        ]
        if len(train_indices) < 150:
            raise RuntimeError(f"{origin_date}: insufficient past-only training rows")
        X_train = all_X.iloc[train_indices].copy()
        y_train = target_matrix[train_indices].copy()
        bundle = fit_range_bundle(candidate, X_train, y_train, calibration_fraction=0.20)

        origin_close = float(feature_rows[origin_date]["features"]["spot_close"])
        forecast_returns = predict_range_bundle(bundle, sample["features"])
        low_return = float(forecast_returns["low_return"])
        high_return = float(forecast_returns["high_return"])
        close_return = float(forecast_returns["close_return"])
        forecast = {
            "origin_date": origin_date,
            "origin_close": origin_close,
            "predicted_low_return": low_return,
            "predicted_high_return": high_return,
            "predicted_close_return": close_return,
            "predicted_low": origin_close * (1.0 + low_return),
            "predicted_high": origin_close * (1.0 + high_return),
            "predicted_close": origin_close * (1.0 + close_return),
            "candidate_name": candidate["name"],
            "calibration_alpha": float(bundle["calibration_alpha"]),
        }
        if not (
            0 < forecast["predicted_low"] <= forecast["predicted_close"]
            <= forecast["predicted_high"]
        ):
            raise RuntimeError(f"{origin_date}: model produced invalid price bounds")

        baseline = make_recent_22_session_baseline(
            feature_rows, origin_date, origin_close
        )
        actual = feature_rows[target_date]["features"]
        record = _score_pair(forecast, baseline, target_date, actual)
        record["training_rows"] = len(train_indices)
        record["estimator_fit_rows"] = int(bundle["fit_samples"])
        record["conformal_calibration_rows"] = int(bundle["calibration_samples"])
        record["training_data_through_date"] = origin_date
        records.append(record)
        fit_counts.append(len(train_indices))

    summary = summarize_shadow_records(records)
    historical_status = (
        "pass_manual_review"
        if summary.get("shadow_validation_status") == "pass_manual_review"
        and summary.get("scored_sessions") == sessions
        else "fail"
    )
    output = {
        "status": "range_40_session_historical_walk_forward_complete",
        "model_group": "Range Intelligence",
        "validation_method": "expanding chronological walk-forward; refit and recalibrate independently at every forecast origin",
        "candidate_locked_from_prior_cv": candidate,
        "candidate_selection_used_test_window": False,
        "test_target_session_count": sessions,
        "test_target_start": records[0]["actual_session_date"],
        "test_target_end": records[-1]["actual_session_date"],
        "forecast_origin_start": records[0]["forecast_origin_date"],
        "forecast_origin_end": records[-1]["forecast_origin_date"],
        "training_policy": "for each origin, only samples with target_date <= origin_date are eligible; the latest 20% of eligible rows calibrate and are excluded from estimator fitting",
        "training_rows_min": int(min(fit_counts)),
        "training_rows_max": int(max(fit_counts)),
        "summary": summary,
        "historical_validation_status": historical_status,
        "historical_pass_criteria": {
            "minimum_scored_sessions": 40,
            "range_coverage_at_least": 0.85,
            "mean_winkler_better_than_22_session_baseline": True,
            "winkler_daily_win_rate_at_least": 0.55,
            "close_mae_no_more_than_baseline_x": 1.10,
        },
        "records": records,
        "b2_reads": 0,
        "b2_writes": 0,
        "deployment_ready": False,
        "production_promotion_allowed": False,
        "promotion_note": "Historical walk-forward is a necessary retrospective gate, not a substitute for future live shadow validation and manual review.",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, indent=2, sort_keys=True), encoding="utf-8")
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Leakage-safe 40-session NIFTY Range walk-forward validation.")
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--tuning-report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = run_historical_walk_forward(
        args.checkpoint, args.tuning_report, args.output, sessions=40
    )
    print(json.dumps({
        "status": report["status"],
        "historical_validation_status": report["historical_validation_status"],
        "test_target_session_count": report["test_target_session_count"],
        "test_target_start": report["test_target_start"],
        "test_target_end": report["test_target_end"],
        "summary": report["summary"],
        "b2_reads": report["b2_reads"],
        "b2_writes": report["b2_writes"],
        "deployment_ready": report["deployment_ready"],
    }, indent=2), flush=True)


if __name__ == "__main__":
    main()
