from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
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
)
from storage import b2_client, read_catalog, upload_collection  # noqa: E402


def dates_between(start: date, end: date):
    current = start
    while current <= end:
        # NSE cash/derivatives reports are not published on weekends.
        if current.weekday() < 5:
            yield current
        current += timedelta(days=1)


def archived_and_valid(client, bucket: str, day: date) -> bool:
    key = f"nse/daily/{day.isoformat()}/manifest.json"
    try:
        response = client.get_object(Bucket=bucket, Key=key)
        manifest = json.loads(response["Body"].read().decode("utf-8"))
    except Exception:
        return False
    if manifest.get("core_session_status") != "aligned":
        return False
    results = manifest.get("results", {})
    return all(
        results.get(name, {}).get("report_date") == day.isoformat()
        and results.get(name, {}).get("file")
        for name in CORE_REPORT_NAMES
    )


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
    else:
        status = "blocked_no_common_session"
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
        "results": results,
        "errors": errors,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest


def run(start: date, end: date, output_root: Path, delay: float):
    if end < start:
        raise ValueError("end date must be on or after start date")

    client = b2_client()
    bucket = __import__("os").environ["B2_BUCKET_NAME"]
    catalog = read_catalog(client)
    catalog_dates = {
        item.get("collection_date")
        for item in catalog.get("collections", [])
        if item.get("collection_date")
    }

    output_root.mkdir(parents=True, exist_ok=True)
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
        "no_common_session_dates": [],
        "errors": [],
    }

    for day in dates_between(start, end):
        day_text = day.isoformat()
        summary["processed_weekdays"] += 1
        if day_text in catalog_dates and archived_and_valid(client, bucket, day):
            summary["skipped_existing_sessions"].append(day_text)
            print(f"SKIP existing validated session {day_text}", flush=True)
            continue

        day_dir = output_root / day_text
        day_dir.mkdir(parents=True, exist_ok=True)
        manifest = build_session(day, session, day_dir, delay)
        if manifest["core_session_status"] != "aligned":
            summary["no_common_session_dates"].append(day_text)
            summary["errors"].extend(manifest["errors"])
            print(f"NO ALIGNED CORE SESSION {day_text}", flush=True)
            continue

        entry = upload_collection(day_dir, day_text)
        summary["uploaded_sessions"].append({
            "date": day_text,
            "report_count": entry.get("report_count", 0),
            "archive_bytes": entry.get("archive_bytes", 0),
        })
        catalog_dates.add(day_text)
        print(f"UPLOADED {day_text}: {entry.get('report_count')} reports", flush=True)

    summary["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
    summary["validated_session_count"] = len(summary["uploaded_sessions"]) + len(summary["skipped_existing_sessions"])
    summary["status"] = "complete_for_requested_range" if not summary["no_common_session_dates"] else "partial_needs_review"
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
    if summary["no_common_session_dates"]:
        return 2
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Resumable two-year NSE NIFTY derivatives historical backfill to Backblaze B2.")
    parser.add_argument("--start", default="2024-11-12", help="First date, YYYY-MM-DD")
    parser.add_argument("--end", default="2026-11-12", help="Last date, YYYY-MM-DD")
    parser.add_argument("--output", default="data/historical_backfill")
    parser.add_argument("--delay", type=float, default=0.8, help="Seconds between NSE report requests")
    args = parser.parse_args()
    code = run(date.fromisoformat(args.start), date.fromisoformat(args.end), Path(args.output), args.delay)
    raise SystemExit(code)
