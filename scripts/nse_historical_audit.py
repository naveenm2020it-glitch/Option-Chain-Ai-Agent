from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from botocore.exceptions import ClientError

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.nse_historical_backfill import is_b2_cap_error, validate_remote_archive  # noqa: E402
from storage import b2_client, read_catalog  # noqa: E402
from scripts.nse_ml_features import FEATURE_SCHEMA_VERSION, extract_nifty_features  # noqa: E402


# NSE F&O trading holidays from the exchange's annual circulars. The window
# starts on 2024-11-12, so only holidays inside the requested window are listed.
# Sources are recorded in data/nse_trading_calendar.json.
HOLIDAYS = {
    date.fromisoformat(value)
    for value in (
        "2024-11-15", "2024-11-20", "2024-12-25",
        "2025-02-26", "2025-03-14", "2025-03-31",
        "2025-04-10", "2025-04-14", "2025-04-18",
        "2025-05-01", "2025-08-15", "2025-08-27",
        "2025-10-02", "2025-10-22", "2025-11-05", "2025-12-25",
        "2026-01-15", "2026-01-26", "2026-03-03", "2026-03-26",
        "2026-03-31", "2026-04-03", "2026-04-14", "2026-05-01",
        "2026-05-28", "2026-06-26", "2026-09-14", "2026-10-02",
        "2026-10-20", "2026-11-10",
    )
}
SPECIAL_SESSIONS = {date(2026, 11, 8)}
CHECKPOINT_VERSION = 2


def expected_sessions(start: date, end: date) -> list[date]:
    """Return weekday F&O sessions minus official holidays plus special sessions."""
    if end < start:
        raise ValueError("end date must be on or after start date")
    result = []
    current = start
    while current <= end:
        if (current.weekday() < 5 and current not in HOLIDAYS) or current in SPECIAL_SESSIONS:
            result.append(current)
        current += timedelta(days=1)
    return result


def _fingerprint(entry: dict) -> str:
    payload = json.dumps(entry, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _load_checkpoint(path: Path, start: date, end: date) -> dict:
    if not path.is_file():
        return {
            "schema_version": CHECKPOINT_VERSION,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "verified": {},
            "failed": {},
            "features": {},
            "feature_errors": {},
            "updated_at_utc": None,
        }
    checkpoint = json.loads(path.read_text(encoding="utf-8"))
    if checkpoint.get("schema_version") != CHECKPOINT_VERSION:
        raise ValueError("Unsupported historical audit checkpoint version")
    if checkpoint.get("start_date") != start.isoformat() or checkpoint.get("end_date") != end.isoformat():
        raise ValueError("Checkpoint date window does not match this audit run")
    if not isinstance(checkpoint.get("verified"), dict) or not isinstance(checkpoint.get("failed"), dict):
        raise ValueError("Checkpoint integrity state is invalid")
    checkpoint.setdefault("features", {})
    checkpoint.setdefault("feature_errors", {})
    if not isinstance(checkpoint["features"], dict) or not isinstance(checkpoint["feature_errors"], dict):
        raise ValueError("Checkpoint feature state is invalid")
    return checkpoint


def _save_report(path: Path, checkpoint: dict, expected: list[date], catalog_dates: set[str],
                 run_state: dict) -> dict:
    expected_text = [item.isoformat() for item in expected]
    verified = checkpoint["verified"]
    failed = checkpoint["failed"]
    catalog_snapshot_current = bool(run_state.get("catalog_snapshot_current", True))
    missing = [day for day in expected_text if day not in catalog_dates] if catalog_snapshot_current else []
    pending = [
        day for day in expected_text
        if day in catalog_dates
        and day not in verified
        and day not in failed
    ]
    all_examined = not pending and not run_state.get("paused_b2_class_b_cap")
    all_verified = (
        all_examined
        and not missing
        and not failed
        and len(verified) == len(expected_text)
    )
    features = checkpoint.get("features", {})
    feature_errors = checkpoint.get("feature_errors", {})
    features_ready = all_verified and len(features) == len(expected_text) and not feature_errors
    if all_verified:
        status = "archive_integrity_complete_calendar_reconciled"
    elif run_state.get("paused_b2_class_b_cap"):
        status = "paused_b2_class_b_cap"
    elif pending:
        status = "audit_in_progress"
    else:
        status = "blocked_missing_or_failed_archives"

    report = {
        "schema_version": CHECKPOINT_VERSION,
        "start_date": checkpoint["start_date"],
        "end_date": checkpoint["end_date"],
        "expected_session_count": len(expected_text),
        "verified_session_count": len(verified),
        "failed_session_count": len(failed),
        "feature_session_count": len(features),
        "feature_error_count": len(feature_errors),
        "feature_errors": feature_errors,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "missing_archive_dates": missing,
        "pending_archive_dates": pending,
        "verified_dates": sorted(verified),
        "failed_dates": failed,
        "processed_this_run": run_state.get("processed_this_run", 0),
        "b2_cap_blocked": bool(run_state.get("paused_b2_class_b_cap")),
        "b2_cap_error": run_state.get("b2_cap_error"),
        "catalog_snapshot_current": catalog_snapshot_current,
        "status": status,
        # The training gate is deliberately closed here. The separate feature
        # dataset and model validation pipeline must also pass before training.
        "model_training_allowed": bool(features_ready),
        "calendar_sources": [
            "https://archives.nseindia.com/content/circulars/FAOP59723.pdf",
            "https://nsearchives.nseindia.com/content/circulars/FAOP64959.pdf",
            "https://nsearchives.nseindia.com/content/circulars/FAOP65588.pdf",
            "https://nsearchives.nseindia.com/content/circulars/FAOP71777.pdf",
            "https://nsearchives.nseindia.com/content/circulars/FAOP72262.pdf",
            "https://www.nseindia.com/resources/exchange-communication-holidays",
        ],
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    checkpoint["updated_at_utc"] = report["updated_at_utc"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(checkpoint, indent=2), encoding="utf-8")
    report_path = path.parent / "coverage.json"
    report_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def run_audit(start: date, end: date, checkpoint_path: Path, max_archives: int = 25) -> dict:
    if max_archives < 1:
        raise ValueError("max_archives must be at least 1")

    expected = expected_sessions(start, end)
    checkpoint = _load_checkpoint(checkpoint_path, start, end)

    # A fully verified checkpoint is terminal: do not make another B2 request.
    if (len(checkpoint["verified"]) == len(expected)
            and not checkpoint["failed"]
            and len(checkpoint.get("features", {})) == len(expected)
            and not checkpoint.get("feature_errors")):
        report_path = checkpoint_path.parent / "coverage.json"
        if report_path.is_file():
            existing = json.loads(report_path.read_text(encoding="utf-8"))
            if existing.get("status") == "archive_integrity_complete_calendar_reconciled":
                return existing

    client = b2_client()
    bucket = os.environ["B2_BUCKET_NAME"]
    try:
        catalog = read_catalog(client)  # exactly one catalog read per audit run
    except ClientError as exc:
        if not is_b2_cap_error(exc):
            raise
        error = exc.response.get("Error", {})
        run_state = {
            "processed_this_run": 0,
            "paused_b2_class_b_cap": True,
            "catalog_snapshot_current": False,
            "b2_cap_error": {
                "code": error.get("Code", "Unknown"),
                "message": str(error.get("Message", exc))[:500],
            },
        }
        prior_dates = set(checkpoint.get("catalog_dates", []))
        report = _save_report(checkpoint_path, checkpoint, expected, prior_dates, run_state)
        print(json.dumps(report, indent=2), flush=True)
        return report
    entries = {
        item.get("collection_date"): item
        for item in catalog.get("collections", [])
        if isinstance(item, dict) and item.get("collection_date")
    }
    catalog_dates = set(entries)
    checkpoint["catalog_dates"] = sorted(catalog_dates)
    run_state = {
        "processed_this_run": 0,
        "paused_b2_class_b_cap": False,
        "catalog_snapshot_current": True,
    }

    for day in expected:
        day_text = day.isoformat()
        entry = entries.get(day_text)
        if entry is None:
            # Missing archives are reported from the calendar/catalog comparison;
            # they are never silently treated as holidays or fetched from NSE here.
            continue

        fingerprint = _fingerprint(entry)
        previous = checkpoint["verified"].get(day_text)
        feature_row = checkpoint["features"].get(day_text)
        feature_error = checkpoint["feature_errors"].get(day_text)
        if (previous and previous.get("catalog_fingerprint") == fingerprint
                and feature_row and feature_row.get("feature_schema_version") == FEATURE_SCHEMA_VERSION):
            continue
        if (previous and previous.get("catalog_fingerprint") == fingerprint
                and feature_error and feature_error.get("catalog_fingerprint") == fingerprint
                and feature_error.get("feature_schema_version") == FEATURE_SCHEMA_VERSION):
            continue
        previous_failure = checkpoint["failed"].get(day_text)
        if previous_failure and previous_failure.get("catalog_fingerprint") == fingerprint:
            continue

        if run_state["processed_this_run"] >= max_archives:
            break

        try:
            # One B2 GET supplies both byte-level validation and feature extraction.
            response = client.get_object(Bucket=bucket, Key=f"nse/daily/{day_text}/reports.zip")
            archive_bytes = response["Body"].read()

            class CachedArchiveClient:
                def get_object(self, **_kwargs):
                    return {"Body": io.BytesIO(archive_bytes)}

            valid, reason = validate_remote_archive(CachedArchiveClient(), bucket, day, entry)
        except ClientError as exc:
            if not is_b2_cap_error(exc):
                raise
            error = exc.response.get("Error", {})
            run_state["paused_b2_class_b_cap"] = True
            run_state["b2_cap_error"] = {
                "code": error.get("Code", "Unknown"),
                "message": str(error.get("Message", exc))[:500],
            }
            break

        run_state["processed_this_run"] += 1
        if valid:
            checkpoint["verified"][day_text] = {
                "catalog_fingerprint": fingerprint,
                "archive_sha256": entry.get("archive_sha256"),
                "archive_bytes": entry.get("archive_bytes"),
                "report_count": entry.get("report_count"),
                "validation": reason,
                "verified_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            checkpoint["failed"].pop(day_text, None)
            try:
                checkpoint["features"][day_text] = extract_nifty_features(archive_bytes, day)
                checkpoint["feature_errors"].pop(day_text, None)
            except Exception as exc:
                checkpoint["features"].pop(day_text, None)
                checkpoint["feature_errors"][day_text] = {
                    "catalog_fingerprint": fingerprint,
                    "feature_schema_version": FEATURE_SCHEMA_VERSION,
                    "reason": f"{type(exc).__name__}: {str(exc)[:500]}",
                    "checked_at_utc": datetime.now(timezone.utc).isoformat(),
                }
        else:
            checkpoint["failed"][day_text] = {
                "catalog_fingerprint": fingerprint,
                "reason": reason,
                "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            checkpoint["verified"].pop(day_text, None)

        # Persist after each remote archive so an interrupted runner never
        # forces already-verified sessions to be downloaded again.
        _save_report(checkpoint_path, checkpoint, expected, catalog_dates, run_state)
        print(f"{'VERIFIED' if valid else 'FAILED'} {day_text}: {reason}", flush=True)

    report = _save_report(checkpoint_path, checkpoint, expected, catalog_dates, run_state)
    print(json.dumps(report, indent=2), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Read-only, resumable integrity audit for the NIFTY historical B2 archive."
    )
    parser.add_argument("--start", default="2024-11-12")
    parser.add_argument("--end", default="2026-11-12")
    parser.add_argument("--checkpoint", default="data/historical_audit/checkpoint.json")
    parser.add_argument("--max-archives", type=int, default=25)
    args = parser.parse_args()
    result = run_audit(
        date.fromisoformat(args.start),
        date.fromisoformat(args.end),
        Path(args.checkpoint),
        args.max_archives,
    )
    # A quota pause is a safe resumable outcome. Missing/corrupt archives remain
    # a non-zero exit only after the entire calendar window has been examined.
    if result["status"] == "blocked_missing_or_failed_archives" and result["failed_session_count"] > 0:
        raise SystemExit(2)
