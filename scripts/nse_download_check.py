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
        ("fo_market_activity", f"/fo/fo{dmy}.zip", "zip"),
        ("fo_futidx_activity", f"/fo/futidx{dmy}.csv", "csv"),
        ("fo_optidx_activity", f"/fo/optidx{dmy}.csv", "csv"),
        ("fo_settlement_prices", f"/archives/nsccl/fo/FOSett_prce_{dmy}.csv", "csv"),
        ("fo_daily_volatility", f"/archives/nsccl/volt/FOVOLT_{dmy}.csv", "csv"),
        ("fo_top10_cm_volume", f"/nsccl/fao_top10cm_to_{dmy}.csv", "csv"),
        ("fo_contract_delta", f"/fo/Contract_Delta_{dmy}.csv", "csv"),
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
    # NSE serves some daily report archives from a different archive host
    # even though the report listing is on nseindia.com. Try both official
    # archive hosts before marking the report unavailable.
    if name == "fo_market_activity":
        filename = path.rsplit("/", 1)[-1]
        urls.extend([
            f"https://archives.nseindia.com/content/fo/{filename}",
            f"https://nseindia.com/content/fo/{filename}",
            f"https://nsearchives.nseindia.com/archives/fo/{filename}",
        ])
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
    for name in [s[0] for s in report_specs(end)]:
        for day in candidate_dates(end, lookback_days):
            spec = next(s for s in report_specs(day) if s[0] == name)
            try:
                payload, meta = fetch_report(session, *spec)
                extension = {"zip": "zip", "gzip": "csv.gz", "csv": "csv"}[spec[2]]
                filename = f"{name}_{day.isoformat()}.{extension}"
                (output / filename).write_bytes(payload)
                results[name] = {"report_date": day.isoformat(), "file": filename, **meta}
                break
            except Exception as exc:
                errors.append({"report": name, "date": day.isoformat(), "error": str(exc)})
        if name not in results:
            results[name] = {"status": "not_found", "checked_days": lookback_days}

    manifest = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "requested_end_date": end.isoformat(),
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
