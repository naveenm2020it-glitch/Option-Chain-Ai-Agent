from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import requests

from scripts.nse_download_check import HEADERS, fetch_report, report_specs
from system1.pipeline import build_package, load_calendar, previous_sessions


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Audit every currently catalogued NSE report source across 23 trading sessions."
    )
    parser.add_argument("--date", default="2025-12-17", help="Selected analysis date YYYY-MM-DD")
    parser.add_argument("--calendar", default="data/nse_trading_calendar.json")
    args = parser.parse_args()

    selected_date = date.fromisoformat(args.date)
    calendar_path = Path(args.calendar)
    calendar = load_calendar(calendar_path)
    sessions = previous_sessions(selected_date, 23, calendar)
    report_names = [name for name, _, _ in report_specs(selected_date)]

    client = requests.Session()
    client.headers.update(HEADERS)
    summary = {
        "analysis_date": selected_date.isoformat(),
        "window_sessions_requested": 23,
        "session_dates_newest_first": sessions,
        "report_sources_in_code": report_names,
        "expected_download_attempts": len(sessions) * len(report_names),
        "report_results": {},
        "sessions": {},
        "b2_calls": 0,
        "render_calls": 0,
        "raw_files_committed": False,
    }
    counts = Counter()
    session_core_ready = 0

    with tempfile.TemporaryDirectory(prefix="system1-23-session-audit-") as temp:
        root = Path(temp)
        for session_text in reversed(sessions):
            session_date = date.fromisoformat(session_text)
            folder = root / session_text
            folder.mkdir(parents=True)
            entries = {}
            per_session = {"reports": {}, "downloaded": 0, "failed": 0}

            for name, path, kind in report_specs(session_date):
                extension = {"zip": "zip", "csv": "csv", "gzip": "csv.gz"}[kind]
                filename = f"{name}_{session_text}.{extension}"
                try:
                    payload, meta = fetch_report(client, name, path, kind)
                    (folder / filename).write_bytes(payload)
                    digest = hashlib.sha256(payload).hexdigest()
                    entries[name] = {
                        "file": filename,
                        "report_date": session_text,
                        "sha256": digest,
                        "download_url": meta["url"],
                        "bytes": len(payload),
                        "http_status": meta["http_status"],
                    }
                    per_session["reports"][name] = {
                        "status": "downloaded",
                        "bytes": len(payload),
                        "sha256": digest,
                        "http_status": meta["http_status"],
                    }
                    per_session["downloaded"] += 1
                    counts[name] += 1
                except Exception as exc:
                    per_session["reports"][name] = {
                        "status": "unavailable",
                        "error": str(exc)[:500],
                    }
                    per_session["failed"] += 1

            manifest = {
                "requested_end_date": session_text,
                "core_session_date": session_text,
                "results": entries,
            }
            (folder / "manifest.json").write_text(
                json.dumps(manifest, indent=2), encoding="utf-8"
            )
            per_session["core_reports_ready"] = all(
                per_session["reports"].get(name, {}).get("status") == "downloaded"
                for name in ("fo_udiff_bhavcopy", "nifty_index_close_all")
            )
            if per_session["core_reports_ready"]:
                session_core_ready += 1
            summary["sessions"][session_text] = per_session

        package = build_package(root, selected_date, calendar_path, 23)
        summary["system1_core_package"] = {
            "status": package["status"],
            "selected_session_ready": package["selected_session_ready"],
            "window_ready_sessions": package["window_ready_sessions"],
            "window_missing_or_blocked_sessions": package["window_missing_or_blocked_sessions"],
            "input_manifest_hash": package["input_manifest_hash"],
        }
        summary["report_results"] = {
            name: {
                "available_sessions": counts[name],
                "total_sessions": 23,
                "missing_sessions": [
                    day for day, item in summary["sessions"].items()
                    if item["reports"].get(name, {}).get("status") != "downloaded"
                ],
            }
            for name in report_names
        }
        summary["core_ready_sessions"] = session_core_ready
        summary["overall_source_audit"] = (
            "ALL_CATALOGUED_SOURCES_AVAILABLE"
            if all(counts[name] == 23 for name in report_names)
            else "SOURCE_GAPS_FOUND"
        )
        print(json.dumps(summary, indent=2))

    # Exit success means the audit executed and the central core package is
    # usable for all 23 sessions. Optional source gaps remain explicit in JSON.
    return 0 if session_core_ready == 23 else 2


if __name__ == "__main__":
    raise SystemExit(main())
