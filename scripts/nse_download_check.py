from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import sys
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

BASE = "https://nsearchives.nseindia.com/content"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
TIMEOUT = 30


def candidate_dates(end: date, days: int = 7):
    for offset in range(days):
        yield end - timedelta(days=offset)


def report_specs(day: date):
    dmy = day.strftime("%d%m%Y")
    ymd = day.strftime("%Y%m%d")
    return [
        # Core NIFTY derivatives data
        ("fo_udiff_bhavcopy", f"/fo/BhavCopy_NSE_FO_0_0_0_{ymd}_F_0000.csv.zip", "zip"),
        ("fo_participant_oi", f"/nsccl/fao_participant_oi_{dmy}.csv", "csv"),
        ("fo_participant_volume", f"/nsccl/fao_participant_vol_{dmy}.csv", "csv"),
        ("fo_market_activity", f"/archives/fo/mkt/fo{dmy}.zip", "zip"),
        ("fo_settlement_prices", f"/archives/nsccl/sett/FOSett_prce_{dmy}.csv", "csv"),
        ("fo_daily_volatility", f"/archives/nsccl/volt/FOVOLT_{dmy}.csv", "csv"),
        ("fo_top10_cm_volume", f"/nsccl/fao_top10cm_to_{dmy}.csv", "csv"),
        ("fo_contract_delta", f"/nsccl/Contract_Delta_{dmy}.csv", "csv"),
        ("fo_contract_master", f"/fo/NSE_FO_contract_{dmy}.csv.gz", "gzip"),
        # NIFTY cash/index context
        ("cm_udiff_bhavcopy", f"/cm/BhavCopy_NSE_CM_0_0_0_{ymd}_F_0000.csv.zip", "zip"),
        ("nifty_index_close_all", f"/indices/ind_close_all_{dmy}.csv", "csv"),
    ]


def validate_payload(name: str, payload: bytes, kind: str) -> dict:
    if not payload or len(payload) < 30:
        raise ValueError("empty or suspiciously small response")
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            bad_member = archive.testzip()
            if bad_member:
                raise ValueError(f"corrupt ZIP member: {bad_member}")
            members = archive.namelist()
            if not members:
                raise ValueError("ZIP contains no files")
            first = archive.read(members[0])
            if len(first) < 10:
                raise ValueError("ZIP member is empty")
            return {"members": members[:5], "uncompressed_first_member_bytes": len(first)}
    raw = gzip.decompress(payload) if kind == "gzip" else payload
    text = raw[:4096].decode("utf-8-sig", errors="replace")
    if "<html" in text.lower() or "<!doctype" in text.lower():
        raise ValueError("received an HTML page instead of CSV")
    if "," not in text and ";" not in text:
        raise ValueError("CSV delimiter not detected")
    return {"preview": text.splitlines()[:2]}


def fetch_report(session: requests.Session, name: str, path: str, kind: str):
    urls = [("https://nsearchives.nseindia.com" + path) if path.startswith("/archives/") else (BASE + path)]
    failures = []
    for url in dict.fromkeys(urls):
        try:
            response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            details = validate_payload(name, response.content, kind)
            return response.content, {"url": url, "http_status": response.status_code,
                                      "bytes": len(response.content),
                                      "sha256": hashlib.sha256(response.content).hexdigest(),
                                      **details}
        except Exception as exc:
            failures.append(f"{url}: {exc}")
    raise RuntimeError("all official URL variants failed: " + " | ".join(failures))


CORE_REPORT_NAMES = (
    "fo_udiff_bhavcopy",
    "fo_participant_oi",
    "fo_participant_volume",
    "fo_market_activity",
)


def save_report(output: Path, name: str, day: date, spec, payload: bytes, meta: dict):
    """Persist one validated report and extract index activity files when present."""
    extension = {"zip": "zip", "gzip": "csv.gz", "csv": "csv"}[spec[2]]
    filename = f"{name}_{day.isoformat()}.{extension}"
    (output / filename).write_bytes(payload)
    result = {"report_date": day.isoformat(), "file": filename, **meta}

    if name == "fo_market_activity":
        with zipfile.ZipFile(io.BytesIO(payload)) as archive:
            wanted = {
                f"futidx{day.strftime('%d%m%Y')}.csv".lower(): "fo_futidx_activity",
                f"optidx{day.strftime('%d%m%Y')}.csv".lower(): "fo_optidx_activity",
            }
            extracted = set()
            for member in archive.namelist():
                member_name = Path(member).name.lower()
                if member_name in wanted:
                    derived = wanted[member_name]
                    derived_file = f"{derived}_{day.isoformat()}.csv"
                    content = archive.read(member)
                    validate_payload(derived, content, "csv")
                    (output / derived_file).write_bytes(content)
                    results_entry = {
                        "status": "extracted",
                        "report_date": day.isoformat(),
                        "file": derived_file,
                        "source_archive": filename,
                        "bytes": len(content),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                    extracted.add(derived)
                    yield derived, results_entry
            for derived in ("fo_futidx_activity", "fo_optidx_activity"):
                if derived not in extracted:
                    yield derived, {
                        "status": "not_in_archive",
                        "report_date": day.isoformat(),
                        "source_archive": filename,
                    }
    yield name, result


def run(output: Path, end: date, lookback_days: int = 7):
    output.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers.update(HEADERS)
    # NSE's homepage may return HTTP 403 to cloud runners. Archive hosts can
    # still serve public files directly, so record the homepage result but do
    # not make it a prerequisite for downloading.
    try:
        home = session.get("https://www.nseindia.com/", timeout=TIMEOUT)
        homepage_status = home.status_code
    except requests.RequestException as exc:
        homepage_status = f"unavailable: {exc}"

    results = {}
    errors = []
    specs_by_name = {spec[0]: spec for spec in report_specs(end)}

    # Download all four required derivatives reports from ONE candidate
    # trading session. Never combine independently-found dates.
    aligned_day = None
    for day in candidate_dates(end, lookback_days):
        staged = {}
        day_errors = []
        for name in CORE_REPORT_NAMES:
            spec = next(item for item in report_specs(day) if item[0] == name)
            try:
                payload, meta = fetch_report(session, *spec)
                staged[name] = (spec, payload, meta)
            except Exception as exc:
                day_errors.append({
                    "report": name,
                    "date": day.isoformat(),
                    "error": str(exc),
                    "group": "core_session_alignment",
                })
        if len(staged) == len(CORE_REPORT_NAMES):
            aligned_day = day
            for name, (spec, payload, meta) in staged.items():
                for result_name, result in save_report(output, name, day, spec, payload, meta):
                    results[result_name] = result
            break
        errors.extend(day_errors)

    if aligned_day is None:
        for name in CORE_REPORT_NAMES:
            results[name] = {
                "status": "no_common_session",
                "report_date": None,
                "checked_days": lookback_days,
            }

    # Ancillary reports are useful context but do not determine whether the
    # core NIFTY derivatives set is session-aligned.
    for name, end_spec in specs_by_name.items():
        if name in CORE_REPORT_NAMES:
            continue
        for day in candidate_dates(end, lookback_days):
            spec = next(item for item in report_specs(day) if item[0] == name)
            try:
                payload, meta = fetch_report(session, *spec)
                for result_name, result in save_report(output, name, day, spec, payload, meta):
                    results[result_name] = result
                break
            except Exception as exc:
                errors.append({
                    "report": name,
                    "date": day.isoformat(),
                    "error": str(exc),
                    "group": "ancillary",
                })
        if name not in results:
            results[name] = {"status": "not_found", "checked_days": lookback_days}

    for derived in ("fo_futidx_activity", "fo_optidx_activity"):
        results.setdefault(derived, {"status": "not_found", "checked_days": lookback_days})

    manifest = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_end_date": end.isoformat(),
        "core_session_date": aligned_day.isoformat() if aligned_day else None,
        "core_session_status": "aligned" if aligned_day else "blocked_no_common_session",
        "nse_homepage_status": homepage_status,
        "results": results,
        "errors": errors,
    }
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return manifest

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Verify and download official NSE EOD report files.")
    parser.add_argument("--date", default=date.today().isoformat(), help="latest date to try, YYYY-MM-DD")
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--output", default="data/nse_reports")
    args = parser.parse_args()
    report = run(Path(args.output), date.fromisoformat(args.date), args.lookback_days)
    for key, value in report["results"].items():
        print(f"{key}: {value.get('status', 'downloaded')} {value.get('report_date', '')} {value.get('bytes', '')}")
    if not any(v.get("bytes") for v in report["results"].values()):
        sys.exit("No valid NSE reports downloaded; inspect manifest.json")
