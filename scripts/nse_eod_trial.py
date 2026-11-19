from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120 Safari/537.36",
    "Accept": "*/*",
    "Referer": "https://www.nseindia.com/",
}
TIMEOUT = 30

# The four core reports must all belong to the same exchange session.
CORE = ("fo_udiff_bhavcopy", "fo_participant_oi", "fo_participant_volume", "fo_market_activity")


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


def validate(payload: bytes, kind: str):
    if len(payload) < 30:
        raise ValueError("empty or undersized response")
    if kind == "zip":
        with zipfile.ZipFile(io.BytesIO(payload)) as z:
            if z.testzip() is not None or not any(not i.is_dir() and i.file_size > 10 for i in z.infolist()):
                raise ValueError("invalid or empty ZIP")
    else:
        import gzip
        raw = gzip.decompress(payload) if kind == "gzip" else payload
        sample = raw[:4096].decode("utf-8-sig", errors="replace")
        if "<html" in sample.lower() or "<!doctype" in sample.lower() or ("," not in sample and ";" not in sample):
            raise ValueError("response is not a CSV payload")


def fetch_session(session: requests.Session, day: date, root: Path):
    day_dir = root / day.isoformat()
    day_dir.mkdir(parents=True, exist_ok=True)
    found, errors = {}, []
    for name, url, kind in specs(day):
        try:
            response = session.get(url, timeout=TIMEOUT)
            response.raise_for_status()
            validate(response.content, kind)
            suffix = {"zip": ".zip", "csv": ".csv", "gzip": ".csv.gz"}[kind]
            filename = name + suffix
            (day_dir / filename).write_bytes(response.content)
            found[name] = {
                "status": "downloaded", "report_date": day.isoformat(), "file": filename,
                "source_url": url, "http_status": response.status_code,
                "bytes": len(response.content), "sha256": hashlib.sha256(response.content).hexdigest(),
            }
        except Exception as exc:
            errors.append({"report": name, "error": f"{type(exc).__name__}: {str(exc)[:300]}"})
            found[name] = {"status": "not_found", "report_date": None}
    missing_core = [name for name in CORE if found.get(name, {}).get("status") != "downloaded"]
    return day_dir, found, errors, missing_core


def run(analysis_date: date, sessions: int, expiry: str, output: Path):
    if not 1 <= sessions <= 22:
        raise ValueError("sessions must be between 1 and 22")
    session = requests.Session()
    session.headers.update(HEADERS)
    output.mkdir(parents=True, exist_ok=True)
    records, scan_errors = [], []
    cursor = analysis_date
    scanned = 0

    # Scan calendar days backwards; count only dates where every core report
    # was successfully retrieved for that exact session. No cross-date mixing.
    while len(records) < sessions and scanned < 50:
        if cursor.weekday() < 5:
            day_dir, reports, errors, missing = fetch_session(session, cursor, output)
            scan_errors.extend([{"date": cursor.isoformat(), **e} for e in errors])
            if not missing:
                records.append({
                    "session_date": cursor.isoformat(),
                    "status": "aligned",
                    "reports": reports,
                })
            else:
                # Keep diagnostic detail, but do not count this as a valid session.
                if day_dir.exists():
                    for p in day_dir.iterdir():
                        p.unlink()
                    day_dir.rmdir()
        cursor -= timedelta(days=1)
        scanned += 1

    records.sort(key=lambda x: x["session_date"])
    manifest = {
        "schema_version": 1,
        "purpose": "isolated_eod_download_trial",
        "analysis_date": analysis_date.isoformat(),
        "expiry_reference": expiry,
        "requested_trading_sessions": sessions,
        "aligned_sessions_downloaded": len(records),
        "scan_calendar_days": scanned,
        "core_reports": list(CORE),
        "sessions": records,
        "errors": scan_errors,
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "complete": len(records) == sessions,
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
    (output / "trial-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    # Rebuild once so the archive contains the final manifest. Its hash is
    # emitted externally because embedding a ZIP's own hash is recursive.
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
    print(json.dumps({"complete": result["complete"], "aligned_sessions": result["aligned_sessions_downloaded"],
                      "archive": str(archive), "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest()}, indent=2))
    if not result["complete"]:
        raise SystemExit(f"Only {result['aligned_sessions_downloaded']} of {args.sessions} aligned sessions found.")
