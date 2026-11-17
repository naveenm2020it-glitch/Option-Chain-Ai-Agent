from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.nse_download_check import HEADERS, TIMEOUT, fetch_report, report_specs
from scripts.nse_ml_features import _index_features, _normalised_name, _participant_features

SOURCES = {
    "index_ohlc": "nifty_index_close_all",
    "participant_oi": "fo_participant_oi",
    "participant_volume": "fo_participant_volume",
}


def _csv_frame(payload: bytes, report_name: str) -> pd.DataFrame:
    decoded = payload.decode("utf-8-sig")
    lines = decoded.splitlines()
    header = 0
    if report_name in {"fo_participant_oi", "fo_participant_volume"}:
        for i, line in enumerate(lines[:15]):
            normalized = _normalised_name(line)
            if "clienttype" in normalized and "futureindexlong" in normalized:
                header = i
                break
    elif report_name == "nifty_index_close_all":
        for i, line in enumerate(lines[:15]):
            normalized = _normalised_name(line)
            if "indexname" in normalized and ("closing" in normalized or "close" in normalized):
                header = i
                break
    frame = pd.read_csv(io.StringIO("\n".join(lines[header:])), dtype=str, low_memory=False)
    if frame.empty:
        raise ValueError(f"{report_name}: CSV has no data rows")
    return frame


def _parse_source(name: str, payload: bytes) -> dict[str, float]:
    frame = _csv_frame(payload, name)
    if name == "nifty_index_close_all":
        features = _index_features(frame)
        if not features.get("index_data_available"):
            raise ValueError("NIFTY 50 index OHLC row/columns not present")
        return features
    prefix = "participant_oi" if name == "fo_participant_oi" else "participant_volume"
    features = _participant_features(frame, prefix)
    if not features.get(f"{prefix}_data_available"):
        raise ValueError(f"{name}: required participant categories/columns are incomplete")
    return features


def _refresh_index_derived_features(row_features: dict) -> bool:
    """Recalculate range/location after OHLC context is merged into a checkpoint."""
    if float(row_features.get("index_data_available", 0.0) or 0.0) < 1.0:
        return False
    # spot_close was selected from the index report when it was available;
    # this fallback also repairs older checkpoints that omitted index_close.
    close = float(row_features.get("index_close") or row_features.get("spot_close") or 0.0)
    high = float(row_features.get("spot_high") or 0.0)
    low = float(row_features.get("spot_low") or 0.0)
    open_value = float(row_features.get("spot_open") or 0.0)
    if close <= 0 or low <= 0 or high < max(open_value, low, close) or low > min(open_value, high, close):
        return False
    row_features["index_close"] = close
    row_features["index_range_pct"] = (high - low) / close if high > low else 0.0
    row_features["index_close_location"] = (close - low) / (high - low) if high > low else 0.5
    return True


def run(checkpoint_path: Path, output_dir: Path, delay_seconds: float = 0.35) -> dict:
    if not checkpoint_path.is_file():
        raise FileNotFoundError(f"Verified checkpoint missing: {checkpoint_path}")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    features = checkpoint.get("features")
    if not isinstance(features, dict) or len(features) != 494:
        raise RuntimeError("Repair blocked: expected exactly 494 verified feature rows")

    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(total=5, connect=5, read=5, backoff_factor=1.0,
                  status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=frozenset(["GET"]))
    session.mount("https://", HTTPAdapter(max_retries=retry))

    output_dir.mkdir(parents=True, exist_ok=True)
    report = {
        "schema_version": 1,
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "date_count": len(features),
        "source_results": {key: {"downloaded": 0, "already_present": 0, "unavailable": 0, "invalid": 0, "missing_dates": []}
                           for key in SOURCES},
        "errors": [],
        "b2_requests": 0,
        "repair_complete": False,
        "repair_status": "running",
        "missing_context_dates": {},
        "core_market_coverage": {},
        "optional_context_ready": False,
        "training_allowed": False,
        "training_gate_reason": "Repair has not finished",
        "missing_value_policy": "No imputation: unavailable/invalid source data remains missing and is listed by date; no synthetic values are written.",
    }

    for day in sorted(features):
        row = features[day]
        row_features = row.setdefault("features", {})
        specs = {item[0]: item for item in report_specs(date.fromisoformat(day))}
        for source_key, report_name in SOURCES.items():
            availability_key = {
                "index_ohlc": "index_data_available",
                "participant_oi": "participant_oi_data_available",
                "participant_volume": "participant_volume_data_available",
            }[source_key]
            state = report["source_results"][source_key]
            if float(row_features.get(availability_key, 0.0)) >= 1.0:
                state["already_present"] += 1
                continue
            try:
                spec = specs[report_name]
                payload, meta = fetch_report(session, *spec)
                parsed = _parse_source(report_name, payload)
                if float(parsed.get(availability_key, 0.0)) < 1.0:
                    raise ValueError("source parsed but availability flag is not set")
                row_features.update(parsed)
                state["downloaded"] += 1
                # Keep a compact provenance record; raw NSE reports are stored as
                # workflow artifacts, never sent to B2 by this repair job.
                raw_name = f"{report_name}_{day}.csv"
                (output_dir / raw_name).write_bytes(payload)
                (output_dir / f"{raw_name}.sha256").write_text(
                    hashlib.sha256(payload).hexdigest(), encoding="utf-8"
                )
                row.setdefault("context_sources", {})[source_key] = {
                    "report_date": day, "file": raw_name, "sha256": meta["sha256"],
                    "url": meta["url"], "bytes": meta["bytes"],
                }
            except Exception as exc:
                fetch_failure = (
                    isinstance(exc, requests.RequestException)
                    or "all official URL variants failed" in str(exc)
                    or isinstance(exc, RuntimeError)
                )
                bucket = "unavailable" if fetch_failure else "invalid"
                state[bucket] += 1
                state["missing_dates"].append(day)
                report["errors"].append({
                    "date": day, "source": source_key, "failure_type": bucket,
                    "error": str(exc)[:500]
                })
            if delay_seconds:
                time.sleep(delay_seconds)

    # Context repair may add OHLC after the original 65-feature extraction.
    # Recompute derived range/location fields so stale zero placeholders cannot
    # silently enter model training.
    for day, row in sorted(features.items()):
        row_features = row.get("features", {})
        if float(row_features.get("index_data_available", 0.0) or 0.0) >= 1.0:
            _refresh_index_derived_features(row_features)

    total = len(features)
    coverage = {}
    for source_key, result in report["source_results"].items():
        coverage[source_key] = (
            result["already_present"] + result["downloaded"]
        ) / total if total else 0.0
    report["coverage"] = coverage

    # Validate that every imported report is tied to the same exchange session
    # and that its raw bytes still match the recorded SHA-256 sidecar.
    date_alignment_errors = []
    provenance_verified = {key: 0 for key in SOURCES}
    provenance_from_verified_archive = {key: 0 for key in SOURCES}
    feature_validation_errors = []
    for day, row in sorted(features.items()):
        if row.get("session_date") != day:
            feature_validation_errors.append({"date": day, "error": "session_date_key_mismatch"})
        if row.get("feature_schema_version") != 3:
            feature_validation_errors.append({"date": day, "error": "feature_schema_version_mismatch"})
        row_values = row.get("features", {})
        if float(row_values.get("spot_close", 0.0)) <= 0:
            feature_validation_errors.append({"date": day, "error": "invalid_or_missing_spot_close"})
        if float(row_values.get("ce_contract_count", 0.0)) <= 0 or float(row_values.get("pe_contract_count", 0.0)) <= 0:
            feature_validation_errors.append({"date": day, "error": "NIFTY_CE_PE_contract_coverage_missing"})
        if float(row_values.get("index_data_available", 0.0) or 0.0) >= 1.0:
            close = float(row_values.get("index_close") or row_values.get("spot_close") or 0.0)
            high = float(row_values.get("spot_high") or 0.0)
            low = float(row_values.get("spot_low") or 0.0)
            open_value = float(row_values.get("spot_open") or 0.0)
            if close <= 0 or low <= 0 or high < max(open_value, low, close) or low > min(open_value, high, close):
                feature_validation_errors.append({"date": day, "error": "invalid_index_ohlc"})
            else:
                expected_range = (high - low) / close if high > low else 0.0
                expected_location = (close - low) / (high - low) if high > low else 0.5
                if abs(float(row_values.get("index_range_pct", 0.0)) - expected_range) > 1e-8:
                    feature_validation_errors.append({"date": day, "error": "stale_index_range_pct"})
                if abs(float(row_values.get("index_close_location", 0.0)) - expected_location) > 1e-8:
                    feature_validation_errors.append({"date": day, "error": "stale_index_close_location"})
        source_meta_map = row.get("context_sources", {})
        specs_for_day = {item[0]: item for item in report_specs(date.fromisoformat(day))}
        for source_key, report_name in SOURCES.items():
            availability_key = {
                "index_ohlc": "index_data_available",
                "participant_oi": "participant_oi_data_available",
                "participant_volume": "participant_volume_data_available",
            }[source_key]
            available = float(row_values.get(availability_key, 0.0)) >= 1.0
            source_meta = source_meta_map.get(source_key)
            if not available:
                continue
            if not source_meta:
                # This value was already present in the integrity-verified
                # historical archive, not fabricated by this repair job.
                provenance_from_verified_archive[source_key] += 1
                continue
            spec = specs_for_day[report_name]
            expected_path = spec[1]
            expected_url = (
                "https://nsearchives.nseindia.com" + expected_path
                if expected_path.startswith("/archives/")
                else "https://nsearchives.nseindia.com/content" + expected_path
            )
            if source_meta.get("report_date") != day or source_meta.get("url") != expected_url:
                date_alignment_errors.append({
                    "date": day, "source": source_key,
                    "recorded_date": source_meta.get("report_date"),
                    "url_matches_expected_date": source_meta.get("url") == expected_url,
                })
                continue
            raw_name = Path(str(source_meta.get("file", ""))).name
            raw_path = output_dir / raw_name
            sidecar_path = output_dir / f"{raw_name}.sha256"
            if not raw_path.is_file() or not sidecar_path.is_file():
                date_alignment_errors.append({
                    "date": day, "source": source_key, "error": "raw_report_or_sha256_sidecar_missing"
                })
                continue
            actual_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
            sidecar_hash = sidecar_path.read_text(encoding="utf-8").strip()
            recorded_hash = str(source_meta.get("sha256", ""))
            if actual_hash != recorded_hash or sidecar_hash != recorded_hash:
                date_alignment_errors.append({
                    "date": day, "source": source_key, "error": "sha256_mismatch"
                })
                continue
            provenance_verified[source_key] += 1

    report["date_alignment_errors"] = date_alignment_errors
    report["date_alignment_pass"] = not date_alignment_errors
    report["provenance_verified"] = provenance_verified
    report["provenance_from_verified_archive"] = provenance_from_verified_archive
    report["feature_validation"] = {
        "validated_rows": total,
        "error_count": len(feature_validation_errors),
        "errors": feature_validation_errors[:100],
        "pass": total == 494 and not feature_validation_errors,
    }
    report["missing_context_dates"] = {
        key: value["missing_dates"]
        for key, value in report["source_results"].items()
        if value["missing_dates"]
    }
    report["repair_complete"] = all(
        result["already_present"] + result["downloaded"]
        + result["unavailable"] + result["invalid"] == total
        for result in report["source_results"].values()
    )

    # Preserve the archive audit's existing core-data gate. Index/participant
    # reports are optional context: missing values remain explicitly unavailable
    # and must not be fabricated or block research-candidate training.
    audit_path = checkpoint_path.with_name("coverage.json")
    audit = json.loads(audit_path.read_text(encoding="utf-8")) if audit_path.is_file() else {}
    feature_rows = list(features.values())
    core_market_coverage = {
        "futures": (
            sum(float(row.get("features", {}).get("future_data_available", 0.0)) >= 1.0 for row in feature_rows) / total
            if total else 0.0
        ),
        "nifty_options": (
            sum(
                float(row.get("features", {}).get("ce_contract_count", 0.0)) > 0
                and float(row.get("features", {}).get("pe_contract_count", 0.0)) > 0
                for row in feature_rows
            ) / total if total else 0.0
        ),
    }
    report["core_market_coverage"] = core_market_coverage
    report["optional_context_ready"] = all(value >= 0.95 for value in coverage.values())
    archive_gate = (
        audit.get("status") == "archive_integrity_complete_calendar_reconciled"
        and int(audit.get("verified_session_count", 0)) == 494
        and int(audit.get("feature_session_count", 0)) == 494
        and int(audit.get("feature_error_count", 1)) == 0
        and bool(audit.get("model_training_allowed"))
    )
    core_gate = all(value >= 0.95 for value in core_market_coverage.values())
    report["training_allowed"] = bool(
        report["repair_complete"] and archive_gate and core_gate
        and report["date_alignment_pass"] and report["feature_validation"]["pass"]
    )
    report["training_gate_reason"] = (
        "PASS: archive integrity, 494 feature rows, date alignment and core NIFTY data validated; any optional context gaps remain explicitly flagged."
        if report["training_allowed"] else
        f"BLOCKED: repair_complete={report['repair_complete']}, archive_gate={archive_gate}, core_gate={core_gate}, date_alignment_pass={report['date_alignment_pass']}, feature_validation_pass={report['feature_validation']['pass']}"
    )
    report["repair_status"] = (
        "complete_no_context_gaps" if report["optional_context_ready"]
        else "complete_with_reported_context_gaps"
    )
    report["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    report["checkpoint_sha256"] = hashlib.sha256(
        json.dumps(checkpoint, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()

    checkpoint_path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
    (output_dir / "context_repair_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps({
        "coverage": coverage,
        "repair_complete": report["repair_complete"],
        "repair_status": report["repair_status"],
        "core_market_coverage": core_market_coverage,
        "optional_context_ready": report["optional_context_ready"],
        "training_allowed": report["training_allowed"],
        "training_gate_reason": report["training_gate_reason"],
        "date_alignment_pass": report["date_alignment_pass"],
        "feature_validation": report["feature_validation"],
        "provenance_verified": provenance_verified,
        "missing_context_dates": {k: len(v) for k, v in report["missing_context_dates"].items()},
        "b2_requests": 0,
    }, indent=2), flush=True)
    if not report["repair_complete"] or not report["training_allowed"]:
        raise SystemExit("Repair or historical/core-data gate failed; training is blocked. Missing values were not imputed.")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Repair missing historical NSE context data without B2 access.")
    parser.add_argument("--checkpoint", default="data/historical_audit/checkpoint.json")
    parser.add_argument("--output-dir", default="data/context_repair")
    parser.add_argument("--delay-seconds", type=float, default=0.35)
    args = parser.parse_args()
    run(Path(args.checkpoint), Path(args.output_dir), args.delay_seconds)
