import json
from pathlib import Path

from scripts.audit_nifty_prepared_datasets import audit_datasets


def _row(day, target_day, target, label, model):
    row = {
        "session_date": day,
        "forecast_as_of": "after_close",
        "target_date": target_day,
        "features": {f"f{i}": float(i) for i in range(3)},
        "target": target,
        "target_class": label,
    }
    if model in ("ce_seller", "pe_seller"):
        row["target_metadata"] = {"expiry": "2026-12-31", "matched_contract_count": 5}
    return row


def _write_model(root: Path, model: str, targets: list[float], labels: list[str | None]):
    (root / model).mkdir(parents=True, exist_ok=True)
    split_rows = {"train": [], "validation": [], "holdout": []}
    for i, (target, label) in enumerate(zip(targets, labels)):
        split = "train" if i < 4 else "validation" if i < 6 else "holdout"
        day = f"2026-01-{i + 1:02d}"
        target_day = f"2026-02-{i + 1:02d}"
        split_rows[split].append(_row(day, target_day, target, label, model))
    for split, rows in split_rows.items():
        (root / model / f"{split}.jsonl").write_text(
            "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
        )
    return {split: {"rows": len(rows)} for split, rows in split_rows.items()}


def test_audit_reports_label_imbalance_and_outliers(tmp_path):
    model_splits = {}
    model_splits["direction"] = _write_model(
        tmp_path, "direction", [0.1] * 8, ["UP"] * 8
    )
    model_splits["range"] = _write_model(
        tmp_path, "range", [0.1] * 8, [None] * 8
    )
    model_splits["ce_seller"] = _write_model(
        tmp_path, "ce_seller", [-0.02, -0.03, -0.04, -0.05, -0.01, 0.02, 2.0, -0.95],
        ["SELLER_FAVORABLE"] * 6 + ["BUYER_FAVORABLE"] * 2,
    )
    model_splits["pe_seller"] = _write_model(
        tmp_path, "pe_seller", [-0.02, -0.03, -0.04, -0.05, -0.01, 0.02, 0.03, -0.95],
        ["SELLER_FAVORABLE"] * 6 + ["BUYER_FAVORABLE"] * 2,
    )
    # All model files must use the same feature count; manifest drives the check.
    manifest = {
        "training_started": False,
        "feature_count": 3,
        "prepared_sample_count": 8,
        "source_session_count": 30,
        "source_window": ["2026-01-01", "2026-02-08"],
        "model_splits": {
            model: {
                split: {
                    **summary[split],
                    "first_target_date": None,
                    "last_target_date": None,
                }
                for split in ("train", "validation", "holdout")
            }
            for model, summary in model_splits.items()
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    report = audit_datasets(tmp_path)
    assert report["training_readiness"] == "REVIEW_REQUIRED"
    assert report["models"]["ce_seller"]["quality_metrics"]["extreme_premium_return_count"] == 2
    assert report["models"]["ce_seller"]["seller_label_sensitivity"]["0.010"]["SELLER_FAVORABLE"] == 4
    assert report["quality_warnings"]


def test_audit_rejects_feature_count_mismatch(tmp_path):
    model_splits = {}
    for model in ("direction", "range", "ce_seller", "pe_seller"):
        model_splits[model] = _write_model(tmp_path, model, [0.1] * 8, [None] * 8)
    manifest = {
        "training_started": False,
        "feature_count": 4,
        "prepared_sample_count": 8,
        "model_splits": {
            model: {split: {"rows": summary[split]["rows"]} for split in ("train", "validation", "holdout")}
            for model, summary in model_splits.items()
        },
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    import pytest
    with pytest.raises(ValueError, match="expected 4 features"):
        audit_datasets(tmp_path)
