from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import io
import json
import re
import zipfile
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
TIMEOUT = 30
CORE = ("fo_udiff_bhavcopy",)
# Contract master is required for expiry/strike mapping, but a bad master must
# not erase an otherwise useful FO session. It is tracked as an analysis gate.
CONTRACT_MASTER = "fo_contract_master"


def specs(day: date):
    dmy, ymd = day.strftime("%d%m%Y"), day.strftime("%Y%m%d")
    return [
        ("fo_udiff_bhavcopy", f"https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip", "zip"),
        ("fo_participant_oi", f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_oi_{dmy}.csv", "csv"),
        ("fo_participant_volume", f"https://nsearchives.nseindia.com/content/nsccl/fao_participant_vol_{dmy}.csv", "csv"),
        ("fo_market_activity", f"https://nsearchives.nseindia.com/content/archives/fo/mkt/fo{dmy}.zip", "zip"),
        ("fo_settlement_prices", f"https://nsearchives.nseindia.com/content/archives/nsccl/sett/FOSett_prce_{dmy}.csv", "csv"),
        ("fo_daily_volatility", f"https://nsearchives.nseindia.com/content/archives/nsccl/volt/FOVOLT_{dmy}.csv", "csv"),
        ("fo_top10_cm_volume", f"https://nsearchives.nseindia.com/content/nsccl/fao_top10cm_to_{dmy}.csv", "csv"),
        ("fo_contract_delta", f"https://nsearchives.nseindia.com/content/nsccl/Contract_Delta_{dmy}.csv", "csv"),
        ("fo_contract_master", f"https://nsearchives.nseindia.com/content/fo/NSE_FO_contract_{dmy}.csv.gz", "gzip"),
        ("cm_udiff_bhavcopy", f"https://nsearchives.nseindia.com/content/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip", "zip"),
        ("nifty_index_close_all", f"https://nsearchives.nseindia.com/content/indices/ind_close_all_{dmy}.csv", "csv"),
    ]


def parse_expiry(value):
    """Parse NSE expiry values without assuming seconds vs milliseconds."""
    if value is None:
        return None
    raw = str(value).strip().strip('"').strip("'")
    if not raw or raw.lower() in {"nan", "none", "nat", "-"}:
        return None
    # Support ISO date/time strings before date-only formats. Some NSE files
    # serialize expiry as a timestamp rather than a bare calendar date.
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in (
        "%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d-%b-%Y", "%d-%B-%Y",
        "%d-%m-%Y", "%d/%m/%Y", "%Y%m%d", "%d-%b-%y",
    ):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            pass
    # Numeric epoch values: NSE files may encode timestamps in seconds or ms.
    if re.fullmatch(r"\d+(\.0+)?", raw):
        number = int(float(raw))
        try:
            if number >= 100_000_000_000:
                return datetime.fromtimestamp(number / 1000, tz=timezone.utc).date()
            if number >= 1_000_000_000:
                return datetime.fromtimestamp(number, tz=timezone.utc).date()
            # Excel date serials (only accept plausible modern dates).
            if 40_000 <= number <= 80_000:
                return (date(1899, 12, 30) + timedelta(days=number))
        except (OverflowError, OSError, ValueError):
            return None
    return None


def normalized_header(row):
    return {re.sub(r"[^a-z0-9]", "", str(k).lower()): k for k in row.keys() if k is not None}


def find_column(headers, aliases):
    for alias in aliases:
        key = re.sub(r"[^a-z0-9]", "", alias.lower())
        if key in headers:
            return headers[key]
    return None


def read_csv_bytes(raw: bytes):
    text = raw.decode("utf-8-sig", errors="replace")
    sample = text[:8192]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;|\t")
    except csv.Error:
        dialect = csv.excel
    reader = csv.DictReader(io.StringIO(text), dialect=dialect)
    return reader.fieldnames or [], list(reader)


def inspect_option_rows(rows, headers, report_day):
    h = normalized_header({name: None for name in headers})
    symbol_col = find_column(h, ("TckrSymb", "Symbol", "Underlying", "UnderlyingSymbol"))
    expiry_col = find_column(h, ("XpryDt", "Expiry", "ExpiryDate", "FinInstrmActlXpryDt", "ActualExpiry"))
    option_col = find_column(h, ("OptnTp", "OptionType", "Option_Type", "InstrumentType"))
    if not symbol_col or not expiry_col:
        return {
            "valid": False, "reason": "required symbol/expiry columns not found",
            "headers": headers[:80], "symbol_column": symbol_col, "expiry_column": expiry_col,
        }
    selected = []
    for row in rows:
        symbol = str(row.get(symbol_col, "")).strip().upper()
        opt = str(row.get(option_col, "")).strip().upper() if option_col else ""
        if symbol == "NIFTY" and (not option_col or opt in {"CE", "PE", "OPTIDX", "OPTSTK"}):
            selected.append(row)
    raw_values = [str(r.get(expiry_col, "")).strip() for r in selected if str(r.get(expiry_col, "")).strip()]
    parsed = [parse_expiry(v) for v in raw_values]
    good = [d for d in parsed if d is not None]
    future = sorted({d.isoformat() for d in good if d >= report_day})
    years = Counter(str(d.year) for d in good)
    return {
        "valid": bool(selected and good and future),
        "reason": None if selected and good and future else "no NIFTY option rows with plausible current/future expiry",
        "headers": headers[:80],
        "symbol_column": symbol_col,
        "expiry_column": expiry_col,
        "option_type_column": option_col,
        "nifty_option_rows": len(selected),
        "expiry_raw_samples": raw_values[:12],
        "expiry_parse_failures": sum(d is None for d in parsed),
        "expiry_year_counts": dict(years),
        "current_or_future_expiries": future,
        "min_parsed_expiry": min(good).isoformat() if good else None,
        "max_parsed_expiry": max(good).isoformat() if good else None,
    }


def inspect_contract_master(payload: bytes, report_day: date, selected_expiry: str):
    try:
        raw = gzip.decompress(payload)
    except OSError as exc:
        return {"valid": False, "reason": f"gzip decompression failed: {exc}"}
    headers, rows = read_csv_bytes(raw)
    result = inspect_option_rows(rows, headers, report_day)
    result["selected_expiry_present"] = selected_expiry in result.get("current_or_future_expiries", [])
    return result


def inspect_fo_bhavcopy(payload: bytes, report_day: date, selected_expiry: str):
    try:
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            bad = archive.testzip()
            if bad:
                return {"valid": False, "reason": f"corrupt ZIP member: {bad}"}
            members = [n for n in archive.namelist() if n.lower().endswith(".csv")]
            if not members:
                return {"valid": False, "reason": "ZIP contains no CSV"}
            member = max(members, key=lambda n: archive.getinfo(n).file_size)
            headers, rows = read_csv_bytes(archive.read(member))
    except Exception as exc:
        return {"valid": False, "reason": f"FO ZIP/CSV parse failed: {type(exc).__name__}: {exc}"}
    result = inspect_option_rows(rows, headers, report_day)
    result["member"] = member
    result["row_count"] = len(rows)
    if result.get("valid"):
        result["selected_expiry_present"] = selected_expiry in result["current_or_future_expiries"]
    return result


def validate_payload(payload: bytes, kind: str):
    if len(payload) < 30:
        raise ValueError("empty or undersized response")
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            if z.testzip() is not None or not any(not i.is_dir() and i.file_size > 10 for i in z.infolist()):
                raise ValueError("invalid or empty ZIP")
    elif kind == "gzip":
        raw = gzip.decompress(payload)
        sample = raw[:4096].decode("utf-8-sig", errors="replace")
        if "<html" in sample.lower() or "<!doctype" in sample.lower() or "," not in sample:
            raise ValueError("gzip response is not a CSV payload")
    else:
        sample = payload[:4096].decode("utf-8-sig", errors="replace")
        if "<html" in sample.lower() or "<!doctype" in sample.lower() or ("," not in sample and ";" not in sample):
            raise ValueError("response is not a CSV payload")


def fetch_session(session: requests.Session, day: date, root: Path, selected_expiry: str):
    day_dir = root / day.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    found, errors = {}, []
    for name, url, kind in specs(day):
        try:
            response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            validate_payload(response.content, kind)
            suffix = {"zip": ".zip", "csv": ".csv", "gzip": ".csv.gz"}[kind]
            filename = name + suffix
            (day_dir / filename).write_bytes(response.content)
            details = {
                "status": "downloaded", "report_date": day.isoformat(), "file": filename,
                "source_url": url, "http_status": response.status_code,
                "bytes": len(response.content), "sha256": hashlib.sha256(response.content).hexdigest(),
            }
            if name == "fo_contract_master":
                details["semantic_validation"] = inspect_contract_master(response.content, day, selected_expiry)
                details["analysis_usable"] = details["semantic_validation"].get("valid", False)
            elif name == "fo_udiff_bhavcopy":
                details["semantic_validation"] = inspect_fo_bhavcopy(response.content, day, selected_expiry)
                details["analysis_usable"] = details["semantic_validation"].get("valid", False)
            else:
                details["analysis_usable"] = True
            found[name] = details
        except Exception as exc:
            errors.append({"report": name, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            found[name] = {"status": "not_found", "report_date": day.isoformat(), "analysis_usable": False}
    missing_core = [name for name in CORE if found.get(name, {}).get("analysis_usable") is not True]
    return day_dir, found, errors, missing_core


def run(analysis_date: date, sessions: int, expiry: str, output: Path):
    if not 1 <= sessions <= 22:
        raise ValueError("sessions must be between 1 and 22")
    try:
        selected_expiry = date.fromisoformat(expiry).isoformat()
    except ValueError as exc:
        raise ValueError("expiry must be YYYY-MM-DD") from exc

    session = requests.Session()
    session.headers.update(HEADERS)
    output.mkdir(parents=True, exist_ok=True)
    records, scan_errors, skipped_dates = [], [], []
    report_coverage = {name: {"downloaded": 0, "missing": 0, "semantically_invalid": 0} for name, _, _ in specs(analysis_date)}
    cursor = analysis_date
    scanned_weekdays = 0

    # Count only sessions with a semantically valid FO Bhavcopy. Weekends and
    # NSE holidays are recorded as skipped dates, not treated as missing sessions.
    while len(records) < sessions and scanned_weekdays < 50:
        if cursor.weekday() < 5:
            scanned_weekdays += 1
            day_dir, reports, errors, missing_core = fetch_session(session, cursor, output, selected_expiry)
            scan_errors.extend([{"date": cursor.isoformat(), **e} for e in errors])
            fo_ok = not missing_core
            if fo_ok:
                for report_name, report_info in reports.items():
                    state = "downloaded" if report_info.get("status") == "downloaded" else "missing"
                    report_coverage[report_name][state] += 1
                    if report_info.get("analysis_usable") is False and state == "downloaded":
                        report_coverage[report_name]["semantically_invalid"] += 1
                records.append({
                    "session_date": cursor.isoformat(),
                    "status": "aligned",
                    "selected_expiry_present_in_fo": reports["fo_udiff_bhavcopy"]["semantic_validation"].get("selected_expiry_present"),
                    "contract_master_usable": reports.get(CONTRACT_MASTER, {}).get("analysis_usable", False),
                    "selected_expiry_present_in_master": reports.get(CONTRACT_MASTER, {}).get("semantic_validation", {}).get("selected_expiry_present", False),
                    "reports": reports,
                })
            else:
                skipped_dates.append({
                    "date": cursor.isoformat(),
                    "reason": "FO Bhavcopy unavailable or semantically invalid",
                    "reports": reports,
                })
                # Keep the diagnostic directory for rejected dates; do not erase
                # partial downloads that may explain an NSE endpoint/schema issue.
        cursor -= timedelta(days=1)

    records.sort(key=lambda x: x["session_date"])
    latest_record = max(records, key=lambda r: r["session_date"]) if records else None
    analysis_ready = bool(
        len(records) == sessions
        and latest_record
        and latest_record.get("contract_master_usable")
        and latest_record.get("selected_expiry_present_in_master")
        and latest_record.get("selected_expiry_present_in_fo")
    )
    manifest = {
        "schema_version": 2,
        "purpose": "isolated_eod_data_quality_trial",
        "analysis_date": analysis_date.isoformat(),
        "selected_expiry": selected_expiry,
        "requested_trading_sessions": sessions,
        "aligned_sessions_downloaded": len(records),
        "weekdays_scanned": scanned_weekdays,
        "core_reports": list(CORE),
        "critical_mapping_report": CONTRACT_MASTER,
        "report_coverage_over_valid_sessions": report_coverage,
        "contract_master_valid_sessions": sum(bool(r.get("contract_master_usable")) for r in records),
        "latest_session_expiry_match": {
            "contract_master": bool(latest_record and latest_record.get("selected_expiry_present_in_master")),
            "fo_bhavcopy": bool(latest_record and latest_record.get("selected_expiry_present_in_fo")),
        },
        "sessions": records,
        "skipped_dates": skipped_dates,
        "errors": scan_errors,
        "analysis_ready": analysis_ready,
        "complete": len(records) == sessions,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "safety": {
            "production_catalog_modified": False,
            "existing_archive_keys_overwritten": False,
            "upload_prefix": "trial-downloads/<analysis-date>/<run-id>/",
        },
    }
    (output / "trial-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    archive = output.parent / f"nifty-eod-trial-{analysis_date.isoformat()}.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(output.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(output))
    return manifest, archive


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--analysis-date", required=True)
    parser.add_argument("--sessions", type=int, default=22)
    parser.add_argument("--expiry", required=True)
    parser.add_argument("--output", default="trial-output")
    args = parser.parse_args()
    result, archive = run(date.fromisoformat(args.analysis_date), args.sessions, args.expiry, Path(args.output))
    print(json.dumps({
        "complete": result["complete"],
        "analysis_ready": result["analysis_ready"],
        "aligned_sessions": result["aligned_sessions_downloaded"],
        "archive": str(archive),
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
    }, indent=2))
    if not result["complete"]:
        print(f"WARNING: only {result['aligned_sessions_downloaded']} of {args.sessions} valid sessions found.")
    if not result["analysis_ready"]:
        print("WARNING: one or more sessions have invalid/missing contract-master mappings; do not use for analysis.")
