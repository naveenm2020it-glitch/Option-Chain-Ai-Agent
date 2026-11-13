from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from botocore.exceptions import ClientError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.nse_download_check import (  # noqa: E402
    CORE_REPORT_NAMES,
    HEADERS,
    fetch_report,
    report_specs,
    save_report,
    validate_payload,
)
from storage import b2_client, read_catalog, upload_collection  # noqa: E402


# NSE F&O special sessions that fall outside the normal Monday-Friday calendar.
# Keep these explicit and source them from official NSE circulars.
SPECIAL_TRADING_SESSIONS = {
    date(2026, 11, 8),  # Diwali Laxmi Pujan / Muhurat Trading (Sunday)
}


def dates_between(start: date, end: date):
    current = start
    while current <= end:
        # Include regular weekdays and explicitly declared weekend special sessions.
        if current.weekday() < 5 or current in SPECIAL_TRADING_SESSIONS:
            yield current
        current += timedelta(days=1)


def archived_and_valid(client, bucket: str, day: date, catalog_entry: dict | None = None) -> bool:
    if not catalog_entry:
        return False
    valid, reason = validate_remote_archive(client, bucket, day, catalog_entry)
    if not valid:
        print(f"ARCHIVE REVALIDATION REQUIRED {day.isoformat()}: {reason}", flush=True)
    return valid



def validate_remote_archive(client, bucket: str, day: date, catalog_entry: dict, manifest: dict | None = None) -> tuple[bool, str]:
    """Download and fully validate one archived session before trusting it."""
    prefix = f"nse/daily/{day.isoformat()}"
    try:
        # One B2 GET per session: manifest.json is already embedded in reports.zip.
        response = client.get_object(Bucket=bucket, Key=f"{prefix}/reports.zip")
        archive_bytes = response["Body"].read()
        if not archive_bytes:
            return False, "empty_archive"
        expected_size = catalog_entry.get("archive_bytes")
        expected_hash = catalog_entry.get("archive_sha256")
        if not expected_size or len(archive_bytes) != int(expected_size):
            return False, "archive_size_mismatch_or_missing_catalog_size"
        digest = hashlib.sha256(archive_bytes).hexdigest()
        if not expected_hash or digest.lower() != str(expected_hash).lower():
            return False, "archive_sha256_mismatch_or_missing_catalog_hash"

        specs = {item[0]: item[2] for item in report_specs(day)}
        required = set(CORE_REPORT_NAMES)
        with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
            bad_member = archive.testzip()
            if bad_member:
                return False, f"zip_crc_failure:{bad_member}"
            members = set(archive.namelist())
            if len(members) != len(archive.namelist()):
                return False, "duplicate_archive_member_names"
            if "manifest.json" not in members:
                return False, "embedded_manifest_missing"
            try:
                manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            except Exception as exc:
                return False, f"embedded_manifest_invalid:{type(exc).__name__}"
            if manifest.get("core_session_status") != "aligned":
                return False, "manifest_not_aligned"
            if manifest.get("core_session_date") != day.isoformat():
                return False, "manifest_session_date_mismatch"
            results = manifest.get("results", {})
            if not required.issubset(results):
                return False, "core_manifest_entries_missing"
            for name in required:
                item = results.get(name, {})
                if item.get("report_date") != day.isoformat() or not item.get("file"):
                    return False, f"core_manifest_date_or_file_invalid:{name}"

            checked_files = 0
            for report_name, item in results.items():
                filename = item.get("file")
                if not filename:
                    continue
                if Path(filename).name != filename or filename not in members:
                    return False, f"manifest_file_missing_or_unsafe:{report_name}"
                if item.get("report_date") != day.isoformat():
                    return False, f"report_session_date_mismatch:{report_name}"
                payload = archive.read(filename)
                if not payload:
                    return False, f"empty_report_payload:{report_name}"
                expected_report_hash = item.get("sha256")
                if not expected_report_hash:
                    return False, f"report_sha256_missing:{report_name}"
                if hashlib.sha256(payload).hexdigest().lower() != str(expected_report_hash).lower():
                    return False, f"report_sha256_mismatch:{report_name}"
                kind = specs.get(report_name, "csv")
                validate_payload(report_name, payload, kind)
                checked_files += 1
            if checked_files < len(required):
                return False, "not_all_core_payloads_checked"
        return True, f"verified_{checked_files}_report_files"
    except ClientError:
        # B2 service errors (especially account download/transaction caps) are
        # operational failures, not evidence of a corrupt archive. Stop the run
        # immediately instead of re-downloading NSE data and attempting uploads.
        raise
    except Exception as exc:
        return False, f"archive_validation_error:{type(exc).__name__}:{str(exc)[:300]}"


def catalog_entries_by_date(catalog: dict) -> dict[str, dict]:
    return {
        item.get("collection_date"): item
        for item in catalog.get("collections", [])
        if item.get("collection_date")
    }

def build_session(day: date, session: requests.Session, output: Path, delay: float):
    results = {}
    errors = []
    staged = {}
    specs = {item[0]: item for item in report_specs(day)}
    for index, name in enumerate(CORE_REPORT_NAMES):
        try:
            payload, meta = fetch_report(session, *specs[name])
            staged[name] = (specs[name], payload, meta)
        except Exception as exc:
            errors.append({
                "report": name,
                "date": day.isoformat(),
                "error": str(exc)[:1000],
                "group": "core_session_alignment",
            })
        if delay and index < len(CORE_REPORT_NAMES) - 1:
            time.sleep(delay)

    if len(staged) == len(CORE_REPORT_NAMES):
        for name, (spec, payload, meta) in staged.items():
            for result_name, result in save_report(output, name, day, spec, payload, meta):
                results[result_name] = result
        status = "aligned"
        day_type = "trading_session"
    else:
        status = "blocked_no_common_session"
        day_type = "incomplete_session" if "fo_udiff_bhavcopy" in staged else "non_trading_or_archive_unavailable"
        for name in CORE_REPORT_NAMES:
            results.setdefault(name, {
                "status": "not_found",
                "report_date": None,
                "checked_date": day.isoformat(),
            })

    manifest = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_end_date": day.isoformat(),
        "core_session_date": day.isoformat() if status == "aligned" else None,
        "core_session_status": status,
        "backfill": True,
        "day_type": day_type,
        "results": results,
        "errors": errors,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def run(start: date, end: date, output_root: Path, delay: float, max_sessions: int | None = None):
    if end < start:
        raise ValueError("end date must be on or after start date")

    output_root.mkdir(parents=True, exist_ok=True)
    client = b2_client()
    bucket = __import__("os").environ["B2_BUCKET_NAME"]
    try:
        catalog = read_catalog(client)
    except ClientError as exc:
        error = exc.response.get("Error", {})
        code = error.get("Code", "Unknown")
        message = error.get("Message", str(exc))
        if "cap exceeded" not in message.lower() and code not in {"AccessDenied", "403"}:
            raise
        blocked = {
            "status": "blocked_b2_class_b_cap",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "model_training_allowed": False,
            "archive_integrity_validation_required": True,
            "error": {"code": code, "message": message[:500]},
            "next_step": "Wait for the B2 daily cap reset or reduce other Class B reads; do not mark archives verified.",
        }
        (output_root / "coverage.json").write_text(json.dumps(blocked, indent=2), encoding="utf-8")
        print(json.dumps(blocked, indent=2), flush=True)
        return 3
    catalog_entries = catalog_entries_by_date(catalog)
    catalog_dates = set(catalog_entries)
    session = requests.Session()
    session.headers.update(HEADERS)
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=4, pool_maxsize=4)
    session.mount("https://", adapter)

    summary = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "processed_weekdays": 0,
        "uploaded_sessions": [],
        "skipped_existing_sessions": [],
        "non_trading_or_unavailable_dates": [],
        "incomplete_session_dates": [],
        "errors": [],
        "archive_integrity_verified": [],
        "archive_integrity_failures": [],
        "max_sessions_per_run": max_sessions,
        "batch_complete": True,
    }

    batch_sessions = 0
    next_unprocessed_date = None
    for day in dates_between(start, end):
        if max_sessions is not None and batch_sessions >= max_sessions:
            summary["batch_complete"] = False
            next_unprocessed_date = day.isoformat()
            break
        batch_sessions += 1
        day_text = day.isoformat()
        summary["processed_weekdays"] += 1
        if day_text in catalog_dates and archived_and_valid(client, bucket, day, catalog_entries.get(day_text)):
            summary["skipped_existing_sessions"].append(day_text)
            summary["archive_integrity_verified"].append(day_text)
            print(f"SKIP existing ZIP-integrity-verified session {day_text}", flush=True)
            continue

        day_dir = output_root / day_text
        day_dir.mkdir(parents=True, exist_ok=True)
        manifest = build_session(day, session, day_dir, delay)
        if manifest["core_session_status"] != "aligned":
            if manifest.get("day_type") == "incomplete_session":
                summary["incomplete_session_dates"].append(day_text)
                summary["errors"].extend(manifest["errors"])
                print(f"INCOMPLETE CORE SESSION {day_text}", flush=True)
            else:
                summary["non_trading_or_unavailable_dates"].append(day_text)
                print(f"NO FO BHAVCOPY (holiday or archive unavailable) {day_text}", flush=True)
            continue

        entry = upload_collection(day_dir, day_text)
        # Verify the uploaded ZIP; embedded manifest avoids a second B2 GET.
        valid, reason = validate_remote_archive(client, bucket, day, entry)
        if not valid:
            summary["archive_integrity_failures"].append({"date": day_text, "reason": reason})
            raise RuntimeError(f"B2 ZIP integrity verification failed for {day_text}: {reason}")
        summary["archive_integrity_verified"].append(day_text)
        summary["uploaded_sessions"].append({
            "date": day_text,
            "report_count": entry.get("report_count", 0),
            "archive_bytes": entry.get("archive_bytes", 0),
        })
        catalog_dates.add(day_text)
        catalog_entries[day_text] = entry
        print(f"UPLOADED and ZIP-verified {day_text}: {entry.get('report_count')} reports", flush=True)

    summary["next_unprocessed_date"] = next_unprocessed_date
    summary["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    validated_dates = sorted(
        [item["date"] for item in summary["uploaded_sessions"]]
        + summary["skipped_existing_sessions"]
    )
    summary["validated_dates"] = validated_dates
    summary["earliest_validated_session"] = validated_dates[0] if validated_dates else None
    summary["latest_validated_session"] = validated_dates[-1] if validated_dates else None
    summary["validated_session_count"] = len(validated_dates)
    summary["archive_integrity_verified_count"] = len(summary["archive_integrity_verified"])
    summary["archive_integrity_validation_required"] = True
    summary["calendar_validation_required"] = True
    summary["model_training_allowed"] = False
    if not summary["batch_complete"]:
        summary["status"] = "batch_incomplete_continue_from_next_unprocessed_date"
    elif max_sessions is not None:
        summary["status"] = "batch_complete_global_validation_pending"
    elif summary["incomplete_session_dates"] or summary["validated_session_count"] < 350 or summary["archive_integrity_failures"] or summary["archive_integrity_verified_count"] != summary["validated_session_count"]:
        summary["status"] = "partial_needs_review"
    else:
        summary["status"] = "uploaded_needs_exchange_calendar_validation"
    key = f"nse/backfill/coverage-{start.isoformat()}-to-{end.isoformat()}.json"
    client.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(summary, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    latest_key = "nse/backfill/latest-coverage.json"
    client.put_object(
        Bucket=bucket,
        Key=latest_key,
        Body=json.dumps(summary, indent=2).encode("utf-8"),
        ContentType="application/json",
    )
    local_report = output_root / "coverage.json"
    local_report.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2), flush=True)
    # No-trade/model-training gate: incomplete weekday coverage must be visible as a failed run.
    # Batch runs are intentionally non-final: a separate cumulative audit must
    # reconcile all batch artifacts before the training gate can ever be opened.
    if max_sessions is not None:
        return 0
    if summary["incomplete_session_dates"] or summary["validated_session_count"] < 350 or summary["archive_integrity_failures"] or summary["archive_integrity_verified_count"] != summary["validated_session_count"]:
        return 2
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resumable two-year NSE NIFTY derivatives historical backfill to Backblaze B2.")
    parser.add_argument("--start", default="2024-11-12", help="First date, YYYY-MM-DD")
    parser.add_argument("--end", default="2026-11-12", help="Last date, YYYY-MM-DD")
    parser.add_argument("--output", default="data/historical_backfill")
    parser.add_argument("--delay", type=float, default=0.8, help="Seconds between NSE report requests")
    parser.add_argument("--max-sessions", type=int, default=25, help="Maximum calendar candidate dates to process in one run; use the reported next_unprocessed_date for the next batch")
    args = parser.parse_args()
    if args.max_sessions < 1:
        parser.error("--max-sessions must be at least 1")
    code = run(date.fromisoformat(args.start), date.fromisoformat(args.end), Path(args.output), args.delay, args.max_sessions)
    raise SystemExit(code)
