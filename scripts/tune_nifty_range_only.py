from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np

from scripts.train_nifty_four_models import (
    EXPECTED_SESSIONS, _build_daily_targets, _check_artifact_gates,
    _feature_matrix, build_rolling_feature_rows,
)
from scripts.tune_nifty_four_models import _load_dependencies, tune_range


def main() -> None:
    parser = argparse.ArgumentParser(description="Range-only NIFTY research tuning.")
    parser.add_argument("--checkpoint", type=Path, default=Path("data/historical_audit/checkpoint.json"))
    parser.add_argument("--audit-report", type=Path, default=Path("data/historical_audit/coverage.json"))
    parser.add_argument("--repair-report", type=Path, default=Path("data/context_repair/context_repair_report.json"))
    parser.add_argument("--output", type=Path, default=Path("models/tuning_artifacts/range_only_report.json"))
    args = parser.parse_args()

    _load_dependencies()
    checkpoint, audit, repair = _check_artifact_gates(args.checkpoint, args.audit_report, args.repair_report)
    feature_rows = checkpoint["features"]
    if len(feature_rows) != EXPECTED_SESSIONS:
        raise RuntimeError("Range tuning requires the verified 494-session archive")
    rolling = build_rolling_feature_rows(feature_rows)
    X, feature_names = _feature_matrix(rolling)
    targets = _build_daily_targets(rolling, feature_rows)
    split = int(len(X) * 0.8)
    y = np.column_stack((targets["low_return"], targets["high_return"], targets["close_return"]))
    result = tune_range(X, y, split)

    report = {
        "status": "range_only_tuning_complete",
        "model_group": "Range Intelligence",
        "archive_sessions": len(feature_rows),
        "daily_samples": len(X),
        "feature_count": len(feature_names),
        "train_samples": split,
        "historical_holdout_samples": len(X) - split,
        "holdout_policy": "Previously inspected holdout is diagnostic only; candidate selection uses expanding-window CV in training segment. No promotion without fresh future shadow validation.",
        "range": result,
        "archive_gates": {
            "audit_training_allowed": audit.get("model_training_allowed"),
            "repair_training_allowed": repair.get("training_allowed"),
            "date_alignment_pass": repair.get("date_alignment_pass"),
            "feature_validation_pass": repair.get("feature_validation", {}).get("pass"),
        },
        "b2_reads": 0,
        "b2_writes": 0,
        "deployment_ready": False,
        "other_models_tuned": False,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(report, indent=2), flush=True)


if __name__ == "__main__":
    main()
