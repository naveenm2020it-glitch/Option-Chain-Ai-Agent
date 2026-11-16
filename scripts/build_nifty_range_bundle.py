from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.range_model_bundle import fit_range_bundle
from scripts.train_nifty_four_models import (
    EXPECTED_SESSIONS,
    _build_daily_targets,
    _check_artifact_gates,
    _feature_matrix,
    build_rolling_feature_rows,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the research-only NIFTY Range candidate bundle.")
    parser.add_argument("--checkpoint", type=Path, default=Path("data/historical_audit/checkpoint.json"))
    parser.add_argument("--audit-report", type=Path, default=Path("data/historical_audit/coverage.json"))
    parser.add_argument("--repair-report", type=Path, default=Path("data/context_repair/context_repair_report.json"))
    parser.add_argument("--tuning-report", type=Path, default=Path("models/tuning_artifacts/range_only_report.json"))
    parser.add_argument("--output-dir", type=Path, default=Path("models/artifacts"))
    args = parser.parse_args()

    checkpoint, audit, repair = _check_artifact_gates(
        args.checkpoint, args.audit_report, args.repair_report
    )
    if not args.tuning_report.is_file():
        raise RuntimeError("Range candidate bundle blocked: tuning report is missing")
    tuning = json.loads(args.tuning_report.read_text(encoding="utf-8"))
    if tuning.get("status") != "range_only_tuning_complete":
        raise RuntimeError("Range candidate bundle blocked: tuning report status is incomplete")
    range_result = tuning.get("range", {})
    if range_result.get("cv_beats_baseline") is not True:
        raise RuntimeError("Range candidate bundle blocked: chronological CV gate did not PASS")
    if tuning.get("b2_reads") != 0 or tuning.get("b2_writes") != 0:
        raise RuntimeError("Range candidate bundle blocked: tuning report violates zero-B2 policy")

    feature_rows = checkpoint["features"]
    if len(feature_rows) != EXPECTED_SESSIONS:
        raise RuntimeError("Range candidate bundle requires the verified 494-session archive")
    rolling = build_rolling_feature_rows(feature_rows)
    X, feature_names = _feature_matrix(rolling)
    targets = _build_daily_targets(rolling, feature_rows)
    y = np.column_stack((targets["low_return"], targets["high_return"], targets["close_return"]))
    candidate = range_result.get("selected_config")
    if not isinstance(candidate, dict) or not candidate.get("family"):
        raise RuntimeError("Range tuning report does not contain a valid selected estimator")

    bundle = fit_range_bundle(candidate, X, y)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    model_path = args.output_dir / "nifty_range_candidate.joblib"
    joblib.dump(bundle, model_path, compress=3)
    digest = hashlib.sha256(model_path.read_bytes()).hexdigest()

    manifest = {
        "status": "range_research_candidate_built",
        "model_version": "nifty-range-candidate-v2",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "archive_start_date": min(feature_rows),
        "archive_end_date": max(feature_rows),
        "archive_sessions": len(feature_rows),
        "daily_samples": len(X),
        "feature_count": len(feature_names),
        "candidate": candidate,
        "selection_cv": range_result.get("selected_cv", {}),
        "selected_minimum_fold_full_range_coverage": range_result.get(
            "selected_minimum_fold_full_range_coverage"
        ),
        "cv_beats_baseline": True,
        "estimator_fit_samples": bundle["fit_samples"],
        "conformal_calibration_samples": bundle["calibration_samples"],
        "calibration_alpha": bundle["calibration_alpha"],
        "normalized_conformal_adjustment": bundle["normalized_adjustment"],
        "normalized_low_adjustment": bundle["normalized_adjustment"]["low"],
        "normalized_high_adjustment": bundle["normalized_adjustment"]["high"],
        "model_file": model_path.name,
        "model_sha256": digest,
        "archive_audit_pass": audit.get("model_training_allowed") is True,
        "context_repair_pass": repair.get("training_allowed") is True,
        "b2_reads": 0,
        "b2_writes": 0,
        "forward_shadow_validation_required": True,
        "deployment_ready": False,
        "production_promotion_allowed": False,
    }
    (args.output_dir / "range_candidate_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2), flush=True)


if __name__ == "__main__":
    main()
