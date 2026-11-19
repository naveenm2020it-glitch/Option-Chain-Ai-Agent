from __future__ import annotations

import argparse
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

MODELS = ("direction", "range", "ce_seller", "pe_seller")
SPLITS = ("train", "validation", "holdout")
SELLER_BANDS = (0.005, 0.01, 0.02, 0.03)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                raise ValueError(f"{path}:{line_number}: blank JSONL row")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_number}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise ValueError(f"{path}:{line_number}: row must be an object")
            rows.append(row)
    return rows


def _finite(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _quantiles(values: list[float]) -> dict[str, float | None]:
    if not values:
        return {key: None for key in ("min", "p01", "p05", "median", "p95", "p99", "max")}
    ordered = sorted(values)

    def percentile(p: float) -> float:
        if len(ordered) == 1:
            return ordered[0]
        position = (len(ordered) - 1) * p
        lower = math.floor(position)
        upper = math.ceil(position)
        if lower == upper:
            return ordered[lower]
        weight = position - lower
        return ordered[lower] * (1 - weight) + ordered[upper] * weight

    return {
        "min": ordered[0],
        "p01": percentile(0.01),
        "p05": percentile(0.05),
        "median": percentile(0.50),
        "p95": percentile(0.95),
        "p99": percentile(0.99),
        "max": ordered[-1],
    }


def _class_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    return dict(sorted(Counter(row.get("target_class") or "UNCLASSIFIED" for row in rows).items()))


def _seller_band_sensitivity(values: list[float]) -> dict[str, dict[str, int]]:
    result = {}
    for band in SELLER_BANDS:
        counts = Counter()
        for value in values:
            label = (
                "SELLER_FAVORABLE" if value < -band else
                "BUYER_FAVORABLE" if value > band else
                "NEUTRAL"
            )
            counts[label] += 1
        result[f"{band:.3f}"] = dict(sorted(counts.items()))
    return result


def audit_datasets(root: Path) -> dict[str, Any]:
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise ValueError("manifest.json is missing")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("training_started") is not False:
        raise ValueError("Phase 2 artifact must explicitly state training_started=false")

    expected_feature_count = manifest.get("feature_count")
    if not isinstance(expected_feature_count, int) or expected_feature_count <= 0:
        raise ValueError("manifest feature_count must be a positive integer")

    report: dict[str, Any] = {
        "audit_schema_version": 1,
        "source_window": manifest.get("source_window"),
        "source_session_count": manifest.get("source_session_count"),
        "prepared_sample_count": manifest.get("prepared_sample_count"),
        "expected_feature_count": expected_feature_count,
        "models": {},
        "structural_errors": [],
        "quality_warnings": [],
        "training_readiness": "NOT_ASSESSED",
    }

    all_model_dates: dict[str, dict[str, list[str]]] = {}
    for model in MODELS:
        model_report = {"splits": {}, "total_rows": 0}
        model_rows: list[dict[str, Any]] = []
        split_dates: dict[str, list[str]] = {}
        for split in SPLITS:
            path = root / model / f"{split}.jsonl"
            if not path.is_file():
                raise ValueError(f"missing dataset file: {path}")
            rows = _read_jsonl(path)
            declared = manifest.get("model_splits", {}).get(model, {}).get(split, {}).get("rows")
            if declared != len(rows):
                raise ValueError(f"{model}/{split}: manifest says {declared}, file has {len(rows)} rows")
            if not rows:
                raise ValueError(f"{model}/{split}: empty split")

            dates = []
            feature_keys = None
            targets = []
            for index, row in enumerate(rows, 1):
                for key in ("session_date", "target_date", "forecast_as_of", "features", "target"):
                    if key not in row:
                        raise ValueError(f"{model}/{split} row {index}: missing {key}")
                if row["forecast_as_of"] != "after_close":
                    raise ValueError(f"{model}/{split} row {index}: forecast_as_of must be after_close")
                if not isinstance(row["session_date"], str) or not isinstance(row["target_date"], str):
                    raise ValueError(f"{model}/{split} row {index}: dates must be ISO strings")
                if row["target_date"] <= row["session_date"]:
                    raise ValueError(f"{model}/{split} row {index}: target_date must follow session_date")
                dates.append(row["target_date"])
                if not isinstance(row["features"], dict):
                    raise ValueError(f"{model}/{split} row {index}: features must be an object")
                keys = set(row["features"])
                if len(keys) != expected_feature_count:
                    raise ValueError(f"{model}/{split} row {index}: expected {expected_feature_count} features, got {len(keys)}")
                if feature_keys is None:
                    feature_keys = keys
                elif keys != feature_keys:
                    raise ValueError(f"{model}/{split}: inconsistent feature names")
                for name, value in row["features"].items():
                    _finite(value, f"{model}/{split} row {index} feature {name}")
                targets.append(_finite(row["target"], f"{model}/{split} row {index} target"))
                if model in ("ce_seller", "pe_seller"):
                    meta = row.get("target_metadata")
                    if not isinstance(meta, dict) or not meta.get("expiry"):
                        raise ValueError(f"{model}/{split} row {index}: seller target metadata/expiry missing")
                    matched = meta.get("matched_contract_count")
                    if not isinstance(matched, int) or matched < 5:
                        raise ValueError(f"{model}/{split} row {index}: matched contract count below five")
            if dates != sorted(dates) or len(dates) != len(set(dates)):
                raise ValueError(f"{model}/{split}: target dates are not strictly chronological and unique")
            model_report["splits"][split] = {
                "rows": len(rows),
                "first_target_date": dates[0],
                "last_target_date": dates[-1],
                "class_counts": _class_counts(rows),
                "target_quantiles": _quantiles(targets),
            }
            model_rows.extend(rows)
            split_dates[split] = dates

        train_end = split_dates["train"][-1]
        validation_start = split_dates["validation"][0]
        validation_end = split_dates["validation"][-1]
        holdout_start = split_dates["holdout"][0]
        if not train_end < validation_start or not validation_end < holdout_start:
            raise ValueError(f"{model}: chronological splits overlap or are out of order")

        model_report["total_rows"] = len(model_rows)
        model_report["class_counts"] = _class_counts(model_rows)
        model_report["target_quantiles"] = _quantiles([_finite(r["target"], f"{model}.target") for r in model_rows])
        report["models"][model] = model_report
        all_model_dates[model] = split_dates

        if model in ("ce_seller", "pe_seller"):
            values = [_finite(r["target"], f"{model}.target") for r in model_rows]
            counts = Counter(r.get("target_class") for r in model_rows)
            expiry_days = []
            for row in model_rows:
                meta = row.get("target_metadata") or {}
                days = meta.get("days_to_expiry_on_target")
                if isinstance(days, bool) or not isinstance(days, int) or days < 0:
                    raise ValueError(f"{model}: invalid days_to_expiry_on_target metadata")
                expiry_days.append(days)
            total = len(values)
            neutral_share = counts.get("NEUTRAL", 0) / total
            non_neutral = [counts.get("SELLER_FAVORABLE", 0), counts.get("BUYER_FAVORABLE", 0)]
            minority_share = min(non_neutral) / total
            extreme_flags = [value <= -0.90 or value >= 1.0 for value in values]
            extreme_count = sum(extreme_flags)
            expiry_horizon_counts = Counter(str(days) for days in expiry_days)
            expiry_day_extreme_count = sum(flag for flag, days in zip(extreme_flags, expiry_days) if days == 0)
            expiry_day_count = sum(days == 0 for days in expiry_days)
            model_report["seller_label_sensitivity"] = _seller_band_sensitivity(values)
            model_report["quality_metrics"] = {
                "neutral_share": neutral_share,
                "minority_directional_class_share": minority_share,
                "extreme_premium_return_count": extreme_count,
                "extreme_premium_return_share": extreme_count / total,
                "expiry_day_target_count": expiry_day_count,
                "expiry_day_target_share": expiry_day_count / total,
                "expiry_day_extreme_target_count": expiry_day_extreme_count,
                "expiry_horizon_days_distribution": dict(sorted(expiry_horizon_counts.items(), key=lambda item: int(item[0]))),
            }
            if neutral_share < 0.05:
                report["quality_warnings"].append(
                    f"{model}: neutral class is only {neutral_share:.1%}; label distribution needs review before supervised training"
                )
            if minority_share < 0.10:
                report["quality_warnings"].append(
                    f"{model}: smaller directional class is only {minority_share:.1%}; class imbalance needs review"
                )
            if expiry_day_count:
                report["quality_warnings"].append(
                    f"{model}: {expiry_day_count}/{total} targets are measured on the option expiry date; keep expiry-day and non-expiry regimes distinguishable"
                )
            if extreme_count:
                report["quality_warnings"].append(
                    f"{model}: {extreme_count}/{total} premium-return targets are <= -90% or >= +100%; "
                    f"{expiry_day_extreme_count} of these occur on expiry day; review expiry-regime behavior and quote continuity before training"
                )

    # All four research datasets must describe the same forecast sessions.
    reference = all_model_dates["direction"]
    for model in MODELS[1:]:
        if all_model_dates[model] != reference:
            raise ValueError(f"{model}: forecast dates differ from direction dataset")

    if sum(report["models"]["direction"]["splits"][s]["rows"] for s in SPLITS) != manifest.get("prepared_sample_count"):
        raise ValueError("prepared_sample_count does not equal exported direction rows")

    report["training_readiness"] = (
        "REVIEW_REQUIRED" if report["quality_warnings"] else "STRUCTURAL_CHECKS_PASSED"
    )
    report["note"] = (
        "This audit checks artifact structure and descriptive target quality. It does not prove "
        "seller identity, calibrate a profitable strategy, validate out-of-sample performance, "
        "or authorize model training. Threshold sensitivity is descriptive only; do not select "
        "a threshold using the holdout set."
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit Phase 2 NIFTY research datasets.")
    parser.add_argument("--input-dir", default="data/prepared_models")
    parser.add_argument("--output", default="data/prepared_models/quality_audit.json")
    args = parser.parse_args()
    report = audit_datasets(Path(args.input_dir))
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, allow_nan=False), encoding="utf-8")
    print(json.dumps(report, indent=2, allow_nan=False))
    if report["structural_errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
