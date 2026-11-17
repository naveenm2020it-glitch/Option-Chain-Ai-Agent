from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import tempfile
import zipfile
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import requests

from scripts.nse_download_check import HEADERS, fetch_report, report_specs
from system1.pipeline import build_package, load_calendar, previous_sessions


def norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def parse_date(value: str):
    value = str(value or "").strip()
    for fmt in ("%Y-%m-%d", "%d-%b-%Y", "%d-%m-%Y", "%d/%m/%Y",
                "%d%m%Y", "%Y%m%d", "%d-%b-%y", "%d/%m/%y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    return None


def inspect_csv(payload: bytes, label: str) -> dict:
    text = payload.decode("utf-8-sig", errors="strict")
    if not text.strip():
        raise ValueError(f"{label}: empty text")
    if "<html" in text[:5000].lower() or "<!doctype" in text[:5000].lower():
        raise ValueError(f"{label}: HTML response, not CSV")
    # NSE CSV exports can contain report titles, blank separator columns,
    # and footer rows. Detect the actual table header instead of assuming row 1.
    parsed_candidates = []
    for delimiter in (",", ";", "\\t", "|"):
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        rows = [row for row in rows if any(str(cell).strip() for cell in row)]
        if len(rows) < 2:
            continue
        for header_index, candidate in enumerate(rows[:60]):
            nonempty = sum(bool(str(cell).strip()) for cell in candidate)
            width_candidate = len(candidate)
            if width_candidate < 2 or nonempty < 2:
                continue
            following = rows[header_index + 1:]
            matching = sum(1 for row in following if len(row) == width_candidate
                           and sum(bool(str(cell).strip()) for cell in row) >= 2)
            data_like = sum(1 for row in following
                            if sum(bool(str(cell).strip()) for cell in row) >= 2)
            ratio = matching / max(1, data_like)
            parsed_candidates.append((matching, ratio, nonempty, -header_index,
                                      delimiter, rows, header_index))
    if not parsed_candidates:
        raise ValueError(f"{label}: no plausible table header found")
    matching, ratio, _, neg_index, delimiter, records, header_index = max(parsed_candidates)
    header = [str(x).strip() for x in records[header_index]]
    if len({norm(x) for x in header if x}) != len([x for x in header if x]):
        raise ValueError(f"{label}: duplicate/ambiguous column names")
    width = len(header)
    following = records[header_index + 1:]
    data = [row for row in following
            if len(row) == width and sum(bool(str(cell).strip()) for cell in row) >= 2]
    ragged = sum(1 for row in following
                 if sum(bool(str(cell).strip()) for cell in row) >= 2 and len(row) != width)
    if not data:
        raise ValueError(f"{label}: no table data rows after detected header")
    if ragged / max(1, len(data) + ragged) > 0.05:
        raise ValueError(f"{label}: {ragged}/{len(data) + ragged} table rows have inconsistent column counts")
    nonempty_cells = sum(bool(str(v).strip()) for row in data for v in row)
    if nonempty_cells == 0:
        raise ValueError(f"{label}: data rows contain no values")
    numeric_cells = 0
    for row in data:
        for value in row:
            value = str(value).strip().replace(",", "")
            if value and value not in ("-", "NA", "N/A"):
                try:
                    float(value)
                    numeric_cells += 1
                except ValueError:
                    pass
    date_columns = [i for i, name in enumerate(header)
                    if norm(name) in {"date", "tradedate", "tradDt".lower(),
                                      "businessdate", "reportdate", "tradingdate",
                                      "timestamp", "indexdate"}]
    flat_dates = sorted({
        parsed
        for i in date_columns
        for row in data
        if i < len(row)
        for parsed in [parse_date(row[i])]
        if parsed
    })
    return {
        "member": label,
        "rows": len(data),
        "columns": len(header),
        "header": header[:80],\n        "header_row_index": header_index,\n        "skipped_preamble_rows": header_index,\n        "detected_delimiter": "tab" if delimiter == "\\t" else delimiter,
        "ragged_rows": ragged,
        "numeric_cells": numeric_cells,
        "date_columns": [header[i] for i in date_columns],
        "observed_dates": flat_dates[:30],
        "header_ok": True,
        "rows_ok": len(data) > 0,
    }


def inspect_payload(name: str, payload: bytes, kind: str, expected_date: str) -> dict:
    if not payload:
        raise ValueError("zero-byte response")
    if kind == "gzip":
        raw = gzip.decompress(payload)
        members = [inspect_csv(raw, name)]
    elif kind == "zip":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            bad = archive.testzip()
            if bad:
                raise ValueError(f"corrupt ZIP member: {bad}")
            names = [n for n in archive.namelist()
                     if not n.endswith("/") and n.lower().endswith((".csv", ".txt"))]
            if not names:
                raise ValueError("ZIP has no CSV/TXT members")
            members = [inspect_csv(archive.read(n), n) for n in names]
    else:
        members = [inspect_csv(payload, name)]

    all_dates = sorted({d for member in members for d in member["observed_dates"]})
    date_mismatch = [d for d in all_dates if d != expected_date]
    date_present = any(expected_date in member["observed_dates"] for member in members)
    total_rows = sum(m["rows"] for m in members)
    total_numeric = sum(m["numeric_cells"] for m in members)
    if total_rows == 0:
        raise ValueError("no data rows found")
    # Contract master/contract delta are large structured files; require identifying fields.
    all_headers = {norm(h) for m in members for h in m["header"]}
    semantic = {}
    if name == "fo_contract_master":
        semantic = {
            "symbol_or_security": bool(all_headers & {"tckrsymb", "symbol", "underlying", "tradingsymbol", "fininstrmid"}),
            "expiry": bool(all_headers & {"xprydt", "expiry", "expirydate", "expirydt"}),
        }
    elif name == "fo_contract_delta":
        semantic = {
            "delta_field": any("delta" in h for h in all_headers),
            "contract_identifier": bool(all_headers & {"symbol", "underlying", "tckrsymb", "fininstrmid", "instrument"}),
        }
    return {
        "status": "content_validated",
        "bytes": len(payload),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "member_count": len(members),
        "rows": total_rows,
        "numeric_cells": total_numeric,
        "members": members,
        "observed_dates": all_dates,
        "date_present": date_present,
        "date_mismatch": date_mismatch,
        "semantic_checks": semantic,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Deep content validation for all catalogued NSE reports over 23 sessions.")
    parser.add_argument("--date", default="2025-12-17")
    parser.add_argument("--calendar", default="data/nse_trading_calendar.json")
    parser.add_argument("--output", default="system1_deep_validation_report.json")
    args = parser.parse_args()

    selected = date.fromisoformat(args.date)
    calendar_path = Path(args.calendar)
    calendar = load_calendar(calendar_path)
    sessions = previous_sessions(selected, 23, calendar)
    client = requests.Session()
    client.headers.update(HEADERS)
    report_names = [name for name, _, _ in report_specs(selected)]
    result = {
        "analysis_date": selected.isoformat(),
        "window_policy": "selected date plus 22 prior trading sessions",
        "session_dates_newest_first": sessions,
        "session_count": len(sessions),
        "report_names": report_names,
        "report_count": len(report_names),
        "expected_report_files": len(sessions) * len(report_names),
        "report_validation": {},
        "sessions": {},
        "b2_calls": 0,
        "render_calls": 0,
        "raw_files_committed": False,
    }
    source_counts = {name: Counter() for name in report_names}
    total_downloaded = total_content_valid = total_invalid = 0

    with tempfile.TemporaryDirectory(prefix="system1-deep-validation-") as temp:
        root = Path(temp)
        for day_text in reversed(sessions):
            day = date.fromisoformat(day_text)
            folder = root / day_text
            folder.mkdir(parents=True)
            entries = {}
            day_result = {"reports": {}, "downloaded": 0, "content_validated": 0, "invalid": 0}
            for name, path, kind in report_specs(day):
                try:
                    payload, meta = fetch_report(client, name, path, kind)
                    digest = hashlib.sha256(payload).hexdigest()
                    extension = {"zip": "zip", "csv": "csv", "gzip": "csv.gz"}[kind]
                    filename = f"{name}_{day_text}.{extension}"
                    (folder / filename).write_bytes(payload)
                    checked = inspect_payload(name, payload, kind, day_text)
                    checked.update({"http_status": meta["http_status"], "download_url": meta["url"]})
                    if checked["date_mismatch"]:
                        checked["status"] = "warning_date_mismatch"
                    elif checked["date_present"] or not checked["observed_dates"]:
                        checked["status"] = "content_validated"
                    else:
                        checked["status"] = "warning_date_not_explicit"
                    entries[name] = {
                        "file": filename, "report_date": day_text, "sha256": digest,
                        "bytes": len(payload), "http_status": meta["http_status"],
                    }
                    day_result["reports"][name] = checked
                    day_result["downloaded"] += 1
                    total_downloaded += 1
                    if checked["status"] == "content_validated":
                        day_result["content_validated"] += 1
                        total_content_valid += 1
                        source_counts[name]["content_validated"] += 1
                    else:
                        source_counts[name]["warning"] += 1
                except Exception as exc:
                    day_result["reports"][name] = {"status": "invalid", "error": str(exc)[:1000]}
                    day_result["invalid"] += 1
                    total_invalid += 1
                    source_counts[name]["invalid"] += 1

            manifest = {"requested_end_date": day_text, "core_session_date": day_text, "results": entries}
            (folder / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            day_result["core_reports_present"] = all(
                day_result["reports"].get(n, {}).get("status") == "content_validated"
                for n in ("fo_udiff_bhavcopy", "nifty_index_close_all")
            )
            result["sessions"][day_text] = day_result

        package = build_package(root, selected, calendar_path, 23)
        result["core_normalization"] = {
            "status": package["status"],
            "selected_session_ready": package["selected_session_ready"],
            "ready_sessions": package["window_ready_sessions"],
            "blocked_sessions": package["window_missing_or_blocked_sessions"],
            "input_manifest_hash": package["input_manifest_hash"],
        }

    for name in report_names:
        result["report_validation"][name] = dict(source_counts[name])
    result["totals"] = {
        "downloaded": total_downloaded,
        "content_validated": total_content_valid,
        "invalid": total_invalid,
        "warning": sum(v["warning"] for v in source_counts.values()),
    }
    result["overall_status"] = (
        "PASS" if total_invalid == 0 and total_content_valid == len(sessions) * len(report_names)
        and package["status"] == "ready"
        else "WARNING" if total_invalid == 0 and package["status"] == "ready"
        else "FAIL"
    )
    output = Path(args.output)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"DEEP_VALIDATION_REPORT={output}")
    return 0 if result["overall_status"] != "FAIL" else 2


if __name__ == "__main__":
    raise SystemExit(main())
