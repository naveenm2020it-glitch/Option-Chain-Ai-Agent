from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import zipfile
from datetime import date, datetime, timezone
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.nse_download_check import CORE_REPORT_NAMES, report_specs, validate_payload
from storage import b2_client, read_catalog


def inspect_and_repair(day: date, output: Path) -> dict:
    checkpoint_path = Path("data/historical_audit/checkpoint.json")
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8")) if checkpoint_path.is_file() else {}
    failure = checkpoint.get("failed", {}).get(day.isoformat(), {})
    if failure.get("reason") != "archive_sha256_mismatch":
        report = {
            "date": day.isoformat(),
            "diagnostic_skipped": True,
            "reason": "checkpoint does not identify the expected catalog SHA-256 mismatch",
            "catalog_repaired": False,
            "b2_reads": 0,
            "b2_writes": 0,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2), encoding="utf-8")
        output_path = os.getenv("GITHUB_OUTPUT")
        if output_path:
            with open(output_path, "a", encoding="utf-8") as handle:
                handle.write("catalog_repaired=false\\n")
        print(json.dumps(report, indent=2), flush=True)
        return report

    client = b2_client()
    bucket = os.environ["B2_BUCKET_NAME"]
    catalog = read_catalog(client)
    entries = {
        item.get("collection_date"): item
        for item in catalog.get("collections", [])
        if isinstance(item, dict) and item.get("collection_date")
    }
    entry = entries.get(day.isoformat())
    if not entry:
        raise RuntimeError(f"No catalog entry for {day.isoformat()}")

    key = entry.get("archive_key") or f"nse/daily/{day.isoformat()}/reports.zip"
    response = client.get_object(Bucket=bucket, Key=key)
    archive_bytes = response["Body"].read()
    actual_sha = hashlib.sha256(archive_bytes).hexdigest()
    metadata_sha = str(response.get("Metadata", {}).get("sha256", "")).lower()
    expected_sha = str(entry.get("archive_sha256", "")).lower()

    checks = {
        "archive_nonempty": bool(archive_bytes),
        "zip_crc_ok": False,
        "manifest_valid": False,
        "core_reports_present": False,
        "report_hashes_valid": False,
        "object_metadata_sha_matches": bool(metadata_sha and metadata_sha == actual_sha),
    }
    errors = []
    if not archive_bytes:
        errors.append("empty_archive")
    else:
        try:
            with zipfile.ZipFile(__import__("io").BytesIO(archive_bytes)) as archive:
                bad_member = archive.testzip()
                checks["zip_crc_ok"] = bad_member is None
                if bad_member:
                    errors.append("zip_crc_failure:" + bad_member)
                members = set(archive.namelist())
                if "manifest.json" not in members:
                    errors.append("embedded_manifest_missing")
                else:
                    manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
                    checks["manifest_valid"] = (
                        manifest.get("core_session_status") == "aligned"
                        and manifest.get("core_session_date") == day.isoformat()
                    )
                    if not checks["manifest_valid"]:
                        errors.append("embedded_manifest_session_not_aligned")
                    results = manifest.get("results", {})
                    checks["core_reports_present"] = isinstance(results, dict) and set(CORE_REPORT_NAMES).issubset(results)
                    if not checks["core_reports_present"]:
                        errors.append("core_report_entries_missing")
                    specs = {item[0]: item[2] for item in report_specs(day)}
                    report_errors = []
                    checked = 0
                    if isinstance(results, dict):
                        for report_name, item in results.items():
                            if not isinstance(item, dict) or not item.get("file"):
                                continue
                            filename = str(item["file"])
                            if PurePosixPath(filename).name != filename or filename not in members:
                                report_errors.append(f"unsafe_or_missing_file:{report_name}")
                                continue
                            if item.get("report_date") != day.isoformat():
                                report_errors.append(f"report_date_mismatch:{report_name}")
                                continue
                            payload = archive.read(filename)
                            expected_report_sha = str(item.get("sha256", "")).lower()
                            if not expected_report_sha or hashlib.sha256(payload).hexdigest().lower() != expected_report_sha:
                                report_errors.append(f"report_sha256_mismatch:{report_name}")
                                continue
                            try:
                                validate_payload(report_name, payload, specs.get(report_name, "csv"))
                            except Exception as exc:
                                report_errors.append(f"report_payload_invalid:{report_name}:{type(exc).__name__}")
                                continue
                            checked += 1
                    checks["report_hashes_valid"] = checked >= len(CORE_REPORT_NAMES) and not report_errors
                    if report_errors:
                        errors.extend(report_errors[:20])
                    checks["checked_report_count"] = checked
        except Exception as exc:
            errors.append(f"archive_parse_error:{type(exc).__name__}:{str(exc)[:300]}")

    internal_ok = all(checks.get(k) for k in (
        "archive_nonempty", "zip_crc_ok", "manifest_valid",
        "core_reports_present", "report_hashes_valid",
    ))
    catalog_repaired = False
    if internal_ok and checks["object_metadata_sha_matches"]:
        entry["archive_sha256"] = actual_sha
        entry["archive_bytes"] = len(archive_bytes)
        catalog["updated_at_utc"] = datetime.now(timezone.utc).isoformat()
        catalog["collections"] = [entries[k] for k in sorted(entries)]
        client.put_object(
            Bucket=bucket,
            Key="nse/catalog.json",
            Body=json.dumps(catalog, indent=2).encode("utf-8"),
            ContentType="application/json",
        )
        catalog_repaired = True
    elif internal_ok:
        errors.append("refused_catalog_update_object_metadata_sha_does_not_match")
    else:
        errors.append("refused_catalog_update_internal_archive_checks_failed")

    report = {
        "date": day.isoformat(),
        "catalog_expected_sha256": expected_sha,
        "actual_archive_sha256": actual_sha,
        "object_metadata_sha256": metadata_sha or None,
        "catalog_expected_bytes": entry.get("archive_bytes"),
        "actual_archive_bytes": len(archive_bytes),
        "checks": checks,
        "errors": errors,
        "internal_archive_valid": internal_ok,
        "catalog_repaired": catalog_repaired,
        "b2_writes": 1 if catalog_repaired else 0,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2), encoding="utf-8")
    output_path = os.getenv("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            handle.write(f"catalog_repaired={'true' if catalog_repaired else 'false'}\\n")
    print(json.dumps(report, indent=2), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Diagnose one NIFTY archive and repair only a stale catalog checksum with independent B2 object metadata confirmation.")
    parser.add_argument("--date", required=True)
    parser.add_argument("--output", default="data/historical_audit/archive_diagnostic.json")
    args = parser.parse_args()
    result = inspect_and_repair(date.fromisoformat(args.date), Path(args.output))
    raise SystemExit(0 if result["catalog_repaired"] else 2)
