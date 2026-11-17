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
from system1.b2_archive import download_archive_from_b2, upload_rolling_archive_to_b2


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


def inspect_csv(payload: bytes, label: str, allow_header_only: bool = False) -> dict:
    text = payload.decode("utf-8-sig", errors="strict")
    if not text.strip():
        raise ValueError(f"{label}: empty text")
    if "<html" in text[:5000].lower() or "<!doctype" in text[:5000].lower():
        raise ValueError(f"{label}: HTML response, not CSV")
    # NSE CSV exports can contain report titles, blank separator columns,
    # and footer rows. Detect the actual table header instead of assuming row 1.
    parsed_candidates = []
    for delimiter in (",", ";", "\t", "|"):
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        rows = [row for row in rows if any(str(cell).strip() for cell in row)]
        if not rows:
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
    if not data and not allow_header_only:
        raise ValueError(f"{label}: no table data rows after detected header")
    if ragged / max(1, len(data) + ragged) > 0.05:
        raise ValueError(f"{label}: {ragged}/{len(data) + ragged} table rows have inconsistent column counts")
    nonempty_cells = sum(bool(str(v).strip()) for row in data for v in row)
    if nonempty_cells == 0 and not allow_header_only:
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
        "header": header[:80],
        "header_row_index": header_index,
        "skipped_preamble_rows": header_index,
        "detected_delimiter": "tab" if delimiter == "\t" else delimiter,
        "ragged_rows": ragged,
        "numeric_cells": numeric_cells,
        "date_columns": [header[i] for i in date_columns],
        "observed_dates": flat_dates[:30],
        "header_ok": True,
        "rows_ok": len(data) > 0,
        "header_only": len(data) == 0,
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

            if name == "fo_market_activity":
                # NSE market-activity archives contain multiple report families.
                # Index futures and index options are mandatory for this project;
                # the VIX-futures member can legitimately be header-only.
                dmy = datetime.strptime(expected_date, "%Y-%m-%d").strftime("%d%m%Y")
                by_basename = {Path(n).name.lower(): n for n in names}
                required = (f"futidx{dmy}.csv", f"optidx{dmy}.csv")
                missing = [member for member in required if member not in by_basename]
                if missing:
                    raise ValueError(
                        "fo_market_activity missing mandatory NIFTY activity member(s): "
                        + ", ".join(missing)
                    )
                members = []
                metadata_members = []
                mandatory_names = set(required)
                for member_name in names:
                    basename = Path(member_name).name.lower()
                    # NSE includes a help/legend text file in this ZIP. It is
                    # documentation, not a market-data table, so record and skip it.
                    if basename == "fohelp.txt":
                        metadata_members.append(member_name)
                        continue
                    is_optional_vix = basename == f"futivx{dmy}.csv"
                    is_mandatory = basename in mandatory_names
                    checked = inspect_csv(
                        archive.read(member_name), member_name,
                        # Mandatory members are checked for non-empty rows below
                        # so their failure is reported at the ZIP/source level.
                        allow_header_only=(is_optional_vix or is_mandatory),
                    )
                    checked["status"] = (
                        "empty_optional" if is_optional_vix and checked["header_only"]
                        else "content_validated"
                    )
                    members.append(checked)
                member_by_name = {Path(m["member"]).name.lower(): m for m in members}
                for required_name in required:
                    if member_by_name[required_name]["rows"] == 0:
                        raise ValueError(
                            f"fo_market_activity mandatory member {required_name} has no data rows"
                        )
            else:
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
        "empty_optional_members": [m["member"] for m in members if m.get("status") == "empty_optional"],
        "metadata_members": metadata_members if kind == "zip" and name == "fo_market_activity" else [],
        "rows": total_rows,
        "numeric_cells": total_numeric,
        "members": members,
        "observed_dates": all_dates,
        "date_present": date_present,
        "date_mismatch": date_mismatch,
        "semantic_checks": semantic,
    }


def audit_normalized_package(package: dict) -> dict:
    sessions = package.get("sessions", {})
    results = {}
    for session, item in sessions.items():
        report = item.get("reports", {}).get("fo_udiff_bhavcopy", {})
        contracts = report.get("contracts", [])
        ce = sum(1 for row in contracts if row.get("option_type") == "CE")
        pe = sum(1 for row in contracts if row.get("option_type") == "PE")
        invalid = []
        for row in contracts:
            expiry = row.get("expiry")
            try:
                parsed_expiry = date.fromisoformat(str(expiry))
                if parsed_expiry < date.fromisoformat(session):
                    invalid.append("expired_contract")
            except (TypeError, ValueError):
                invalid.append("invalid_expiry")
            if row.get("underlying") != "NIFTY" or row.get("option_type") not in ("CE", "PE"):
                invalid.append("invalid_contract_identity")
            for field in ("strike", "close", "oi", "change_oi", "volume"):
                value = row.get(field)
                if not isinstance(value, (int, float)) or not __import__("math").isfinite(value):
                    invalid.append("nonfinite_" + field)
            if row.get("strike", 0) <= 0 or row.get("close", -1) < 0 or row.get("oi", -1) < 0 or row.get("volume", -1) < 0:
                invalid.append("out_of_range_value")
        duplicates = report.get("duplicate_contract_keys", -1)
        passed = (
            item.get("status") == "ready" and ce > 0 and pe > 0
            and duplicates == 0 and bool(contracts) and not invalid
        )
        results[session] = {
            "status": "PASS" if passed else "FAIL",
            "contract_count": len(contracts),
            "ce_contracts": ce,
            "pe_contracts": pe,
            "expiry_count": len({r.get("expiry") for r in contracts}),
            "duplicate_contract_keys": duplicates,
            "validation_errors": sorted(set(invalid)),
        }
    return {
        "status": "PASS" if len(results) == package.get("window_sessions") and all(v["status"] == "PASS" for v in results.values()) else "FAIL",
        "sessions_checked": len(results),
        "sessions": results,
    }


def frozen_package_fingerprint(package: dict) -> str:
    stable = {k: v for k, v in package.items() if k != "generated_at_utc"}
    payload = json.dumps(stable, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description="Deep content validation for catalogued NSE reports across the selected trading-session window.")
    parser.add_argument("--date", default="2025-12-17")
    parser.add_argument("--sessions", type=int, default=23, help="Number of sessions including selected date")
    parser.add_argument("--calendar", default="data/nse_trading_calendar.json")
    parser.add_argument("--output", default="system1_deep_validation_report.json")
    parser.add_argument("--archive-to-b2", action="store_true", help="Reuse and update the validated rolling 23-session archive in B2")
    args = parser.parse_args()

    selected = date.fromisoformat(args.date)
    calendar_path = Path(args.calendar)
    calendar = load_calendar(calendar_path)
    if args.sessions < 1:
        parser.error("--sessions must be at least 1")
    sessions = previous_sessions(selected, args.sessions, calendar)
    client = requests.Session()
    client.headers.update(HEADERS)
    report_names = [name for name, _, _ in report_specs(selected)]
    result = {
        "analysis_date": selected.isoformat(),
        "window_policy": f"selected date plus {args.sessions - 1} prior trading sessions",
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
        rolling_key = "system1/rolling/nifty50-eod-23-session.tar.gz"
        if args.archive_to_b2:
            result["b2_cache"] = download_archive_from_b2(root, rolling_key)
        # Keep only the requested 23-session window; the rolling cache must not
        # accumulate expired sessions indefinitely.
        keep = set(sessions)
        for cached in list(root.iterdir()):
            if cached.is_dir() and cached.name not in keep:
                import shutil
                shutil.rmtree(cached)
        for day_text in reversed(sessions):
            day = date.fromisoformat(day_text)
            folder = root / day_text
            folder.mkdir(parents=True, exist_ok=True)
            cached_manifest = folder / "manifest.json"
            try:
                cached = json.loads(cached_manifest.read_text(encoding="utf-8")) if cached_manifest.is_file() else {}
            except (OSError, json.JSONDecodeError):
                cached = {}
            entries = {}
            day_result = {"reports": {}, "downloaded": 0, "reused": 0, "content_validated": 0, "invalid": 0}
            for name, path, kind in report_specs(day):
                try:
                    cached_entry = cached.get("results", {}).get(name, {})
                    cached_file = folder / str(cached_entry.get("file", ""))
                    if cached_entry.get("report_date") == day_text and cached_file.is_file():
                        payload = cached_file.read_bytes()
                        digest = hashlib.sha256(payload).hexdigest()
                        if cached_entry.get("sha256") != digest:
                            raise ValueError("cached report SHA-256 mismatch")
                        meta = {"http_status": cached_entry.get("http_status", 200),
                                "url": "b2://rolling-cache", "cached": True}
                        day_result["reused"] += 1
                    else:
                        payload, fetched_meta = fetch_report(client, name, path, kind)
                        digest = hashlib.sha256(payload).hexdigest()
                        extension = {"zip": "zip", "csv": "csv", "gzip": "csv.gz"}[kind]
                        filename = f"{name}_{day_text}.{extension}"
                        (folder / filename).write_bytes(payload)
                        cached_entry = {"file": filename, "report_date": day_text,
                                        "sha256": digest, "bytes": len(payload),
                                        "http_status": fetched_meta["http_status"]}
                        meta = fetched_meta
                        day_result["downloaded"] += 1
                        total_downloaded += 1

                    checked = inspect_payload(name, payload, kind, day_text)
                    checked.update({"http_status": meta["http_status"], "download_url": meta["url"]})
                    if checked["date_mismatch"]:
                        checked["status"] = "warning_date_mismatch"
                    elif checked["date_present"] or not checked["observed_dates"]:
                        checked["status"] = "content_validated"
                    else:
                        checked["status"] = "warning_date_not_explicit"
                    entries[name] = cached_entry
                    day_result["reports"][name] = checked
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
            cached_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            day_result["core_reports_present"] = all(
                day_result["reports"].get(n, {}).get("status") == "content_validated"
                for n in ("fo_udiff_bhavcopy", "nifty_index_close_all")
            )
            result["sessions"][day_text] = day_result

        package = build_package(root, selected, calendar_path, args.sessions)
        package_repeat = build_package(root, selected, calendar_path, args.sessions)
        fingerprint = frozen_package_fingerprint(package)
        repeat_fingerprint = frozen_package_fingerprint(package_repeat)
        normalized_audit = audit_normalized_package(package)
        result["core_normalization"] = {
            "status": package["status"],
            "selected_session_ready": package["selected_session_ready"],
            "ready_sessions": package["window_ready_sessions"],
            "blocked_sessions": package["window_missing_or_blocked_sessions"],
            "input_manifest_hash": package["input_manifest_hash"],
        }
        result["normalized_ce_pe_expiry_audit"] = normalized_audit
        result["frozen_package_audit"] = {
            "status": "PASS" if fingerprint == repeat_fingerprint else "FAIL",
            "fingerprint": fingerprint,
            "repeat_fingerprint": repeat_fingerprint,
            "reproducible": fingerprint == repeat_fingerprint,
            "generated_at_utc_excluded_from_fingerprint": True,
        }

        # Archive only after every report, normalized contract, and frozen-package
        # integrity gate has passed. Keep raw reports and the exact package together.
        if args.archive_to_b2:
            archive_ready = (
                total_invalid == 0
                and total_content_valid == len(sessions) * len(report_names)
                and package["window_ready_sessions"] == len(sessions)
                and normalized_audit["status"] == "PASS"
                and fingerprint == repeat_fingerprint
            )
            if not archive_ready:
                raise RuntimeError("B2 archive blocked: the complete 23-session integrity gate did not PASS")
            stable_package = {key: value for key, value in package.items() if key != "generated_at_utc"}
            (root / "system1_frozen_package.json").write_text(
                json.dumps(stable_package, indent=2, sort_keys=True, allow_nan=False), encoding="utf-8"
            )
            archive_manifest = {
                "analysis_date": selected.isoformat(),
                "sessions": sessions,
                "report_names": report_names,
                "expected_reports": len(sessions) * len(report_names),
                "content_validated_reports": total_content_valid,
                "package_fingerprint": fingerprint,
                "normalized_audit": normalized_audit,
                "archive_format": "tar.gz",
            }
            (root / "archive_manifest.json").write_text(
                json.dumps(archive_manifest, indent=2), encoding="utf-8"
            )
            result["b2_archive"] = upload_rolling_archive_to_b2(root, rolling_key)

    for name in report_names:
        result["report_validation"][name] = dict(source_counts[name])
    result["totals"] = {
        "downloaded": total_downloaded,
        "content_validated": total_content_valid,
        "invalid": total_invalid,
        "warning": sum(v["warning"] for v in source_counts.values()),
    }
    result["source_reconciliation"] = {
        "status": "PASS" if total_invalid == 0 and total_content_valid == len(sessions) * len(report_names)
        and sum(v["warning"] for v in source_counts.values()) == 0 else "FAIL",
        "expected_report_files": len(sessions) * len(report_names),
        "content_validated": total_content_valid,
        "invalid": total_invalid,
        "warnings": sum(v["warning"] for v in source_counts.values()),
        "all_report_dates_and_structures_validated": total_invalid == 0 and total_content_valid == len(sessions) * len(report_names),
    }
    all_sessions_ready = package["window_ready_sessions"] == len(sessions)
    normalized_pass = result["normalized_ce_pe_expiry_audit"]["status"] == "PASS"
    frozen_pass = result["frozen_package_audit"]["status"] == "PASS"
    result["overall_status"] = (
        "PASS" if total_invalid == 0 and total_content_valid == len(sessions) * len(report_names)
        and all_sessions_ready and normalized_pass and frozen_pass
        else "WARNING" if total_invalid == 0 and package["status"] == "ready"
        else "FAIL"
    )
    output = Path(args.output)
    output.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    print(f"DEEP_VALIDATION_REPORT={output}")
    return 0 if result["overall_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
