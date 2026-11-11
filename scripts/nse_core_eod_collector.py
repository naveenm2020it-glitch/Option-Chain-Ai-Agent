"""Collect official NSE EOD files used by next-session analysis.

Sources:
- NSE archive UDiFF CM and FO bhavcopies
- NSE archive daily all-indices close file (split into NIFTY 50 and INDIA VIX)
- NSE FII/FPI and DII activity JSON endpoint (daily snapshot)

Only verified source rows/files are archived. No missing value is estimated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
import zipfile
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data" / "nse_reports"
MANIFEST_PATH = DATA_ROOT / "manifest.json"
ARCHIVE = "https://nsearchives.nseindia.com"
NSE = "https://www.nseindia.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/csv, application/zip, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.nseindia.com/all-reports",
    "Connection": "keep-alive",
}
DATASETS = {
    "nifty50": "NIFTY 50 Historical Index",
    "india_vix": "India VIX Historical Index",
    "cm_bhavcopy": "CM UDiFF Common Bhavcopy Final",
    "fo_bhavcopy": "F&O UDiFF Common Bhavcopy Final",
    "fii_dii": "FII/FPI and DII Trading Activity",
}


def _load_manifest():
    if not MANIFEST_PATH.exists():
        return {"schema_version": 1, "source": "NSE official public EOD archives", "datasets": {}}
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("datasets"), dict):
        raise RuntimeError("Existing NSE report manifest has an unexpected schema.")
    return data


def _iso_date(value):
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def _read_index(payload: bytes):
    last = None
    for encoding in ("utf-8-sig", "latin1"):
        try:
            frame = pd.read_csv(BytesIO(payload), encoding=encoding)
            frame.columns = [str(c).replace("\ufeff", "").strip() for c in frame.columns]
            lookup = {str(c).strip().lower(): c for c in frame.columns}
            required = ["index name", "index date", "open index value", "high index value",
                        "low index value", "closing index value"]
            if not all(x in lookup for x in required):
                raise ValueError("NSE index-close file is missing expected OHLC columns.")
            return frame
        except Exception as exc:
            last = exc
    raise ValueError(f"Unable to parse NSE all-indices CSV: {last}")


def _parse_udiff_zip(payload: bytes, kind: str):
    if not payload.startswith(b"PK"):
        raise ValueError("NSE UDiFF response is not a ZIP file.")
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        names = [n for n in archive.namelist() if n.lower().endswith(".csv") and not n.endswith("/")]
        if not names:
            raise ValueError("UDiFF ZIP contains no CSV data file.")
        frames = []
        for name in names:
            frame = pd.read_csv(archive.open(name), low_memory=False)
            frame.columns = [str(c).replace("\ufeff", "").strip() for c in frame.columns]
            frames.append(frame)
    columns = {str(c).replace("_", "").replace(" ", "").lower() for f in frames for c in f.columns}
    if kind == "fo_bhavcopy":
        required = {"tckrsymb", "opntp", "strkpric", "opnintrst"}
        # NSE UDiFF headers use OptnTp (not OpnTp).
        required = {"tckrsymb", "optntp", "strkpric", "opnintrst"}
    else:
        required = {"tckrsymb", "sctysrs", "clspric"}
    if not required.issubset(columns):
        raise ValueError(f"{kind} ZIP does not contain the expected UDiFF columns.")
    return sum(len(f) for f in frames), list(dict.fromkeys(c for f in frames for c in f.columns))[:60]


def _download(session, url, attempts=3):
    last = None
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=(10, 45))
            if response.status_code == 404:
                return None
            response.raise_for_status()
            if not response.content:
                raise ValueError("NSE returned an empty body.")
            return response.content
        except (requests.RequestException, ValueError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Download failed after {attempts} attempts: {last}")


def _write(target: Path, payload: bytes):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_suffix(target.suffix + ".tmp")
    temp.write_bytes(payload)
    temp.replace(target)


def _record(dataset, day, status, **fields):
    dataset.setdefault("records", {})[day] = {"status": status, **fields}


def _display_path(target):
    try:
        return target.relative_to(ROOT).as_posix()
    except ValueError:
        return str(target)


def _archive_file(dataset, day, target, payload, rows, columns, url):
    _write(target, payload)
    _record(dataset, day, "available",
            filename=_display_path(target),
            sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload),
            rows=int(rows), columns=[str(c) for c in columns][:60], source_url=url)


def _collect_date_reports(session, day, manifest, pause):
    iso = day.isoformat()
    token = day.strftime("%Y%m%d")
    token_dmy = day.strftime("%d%m%Y")
    specs = {
        "cm_bhavcopy": (f"{ARCHIVE}/content/cm/BhavCopy_NSE_CM_0_0_0_{token}_F_0000.csv.zip",
                        DATA_ROOT / "cm_bhavcopy" / f"BhavCopy_NSE_CM_0_0_0_{token}_F_0000.csv.zip"),
        "fo_bhavcopy": (f"{ARCHIVE}/content/fo/BhavCopy_NSE_FO_0_0_0_{token}_F_0000.csv.zip",
                        DATA_ROOT / "fo_bhavcopy" / f"BhavCopy_NSE_FO_0_0_0_{token}_F_0000.csv.zip"),
    }
    for kind, (url, target) in specs.items():
        ds = manifest["datasets"].setdefault(kind, {"label": DATASETS[kind], "records": {}})
        existing = ds.get("records", {}).get(iso, {})
        if target.exists():
            try:
                payload = target.read_bytes()
                rows, columns = _parse_udiff_zip(payload, kind)
                _record(ds, iso, "available", filename=_display_path(target),
                        sha256=hashlib.sha256(payload).hexdigest(), bytes=len(payload),
                        rows=rows, columns=columns, source_url=url)
                continue
            except Exception:
                target.unlink(missing_ok=True)
        try:
            payload = _download(session, url)
            if payload is None:
                if existing.get("status") != "not_available":
                    _record(ds, iso, "not_available", source_url=url)
            else:
                rows, columns = _parse_udiff_zip(payload, kind)
                _archive_file(ds, iso, target, payload, rows, columns, url)
        except Exception as exc:
            logging.warning("%s %s: %s", kind, iso, exc)
            if existing.get("status") != "available":
                _record(ds, iso, "error", source_url=url, error=str(exc)[:500])
        pause()


def _collect_indices(session, day, manifest, pause):
    iso = day.isoformat()
    token = day.strftime("%d%m%Y")
    url = f"{ARCHIVE}/content/indices/ind_close_all_{token}.csv"
    try:
        payload = _download(session, url)
        if payload is None:
            for kind in ("nifty50", "india_vix"):
                ds = manifest["datasets"].setdefault(kind, {"label": DATASETS[kind], "records": {}})
                if ds.get("records", {}).get(iso, {}).get("status") != "available":
                    _record(ds, iso, "not_available", source_url=url)
            return
        frame = _read_index(payload)
        name_col = next(c for c in frame.columns if c.lower() == "index name")
        date_col = next(c for c in frame.columns if c.lower() == "index date")
        date_values = frame[date_col].map(_iso_date)
        for kind, wanted in (("nifty50", "NIFTY 50"), ("india_vix", "INDIA VIX")):
            ds = manifest["datasets"].setdefault(kind, {"label": DATASETS[kind], "records": {}})
            subset = frame[frame[name_col].astype(str).str.strip().str.upper() == wanted].copy()
            subset = subset[date_values.loc[subset.index] == iso]
            if subset.empty:
                _record(ds, iso, "not_available", source_url=url,
                        error=f"{wanted} row not present in official all-indices file.")
                continue
            csv_payload = subset.to_csv(index=False).encode("utf-8")
            target = DATA_ROOT / kind / f"{kind}_{token}.csv"
            _archive_file(ds, iso, target, csv_payload, len(subset), list(subset.columns), url)
    except Exception as exc:
        logging.warning("index data %s: %s", iso, exc)
        for kind in ("nifty50", "india_vix"):
            ds = manifest["datasets"].setdefault(kind, {"label": DATASETS[kind], "records": {}})
            if ds.get("records", {}).get(iso, {}).get("status") != "available":
                _record(ds, iso, "error", source_url=url, error=str(exc)[:500])
    finally:
        pause()


def _collect_fii_dii(session, manifest):
    url = f"{NSE}/api/fiidiiTradeReact"
    ds = manifest["datasets"].setdefault("fii_dii", {"label": DATASETS["fii_dii"], "records": {}})
    try:
        response = session.get(url, timeout=(10, 30))
        response.raise_for_status()
        payload = response.json()
        rows = payload if isinstance(payload, list) else payload.get("data", [])
        if not rows:
            raise ValueError("NSE FII/DII endpoint returned no rows.")
        grouped = {}
        for row in rows:
            day = _iso_date(row.get("date"))
            if not day:
                continue
            category = str(row.get("category", "")).strip()
            if not category:
                continue
            def amount(key):
                value = str(row.get(key, "") or "").replace(",", "").strip()
                parsed = pd.to_numeric(value, errors="coerce")
                return None if pd.isna(parsed) else float(parsed)
            grouped.setdefault(day, []).append({
                "Date": day, "Category": category,
                "Buy Value": amount("buyValue"), "Sell Value": amount("sellValue"),
                "Net Value": amount("netValue"),
            })
        if not grouped:
            raise ValueError("NSE FII/DII response contained no dated category rows.")
        for day, values in grouped.items():
            frame = pd.DataFrame(values)
            if not frame[["Buy Value", "Sell Value", "Net Value"]].notna().any().any():
                raise ValueError(f"FII/DII values were not numeric for {day}.")
            csv_payload = frame.to_csv(index=False).encode("utf-8")
            target = DATA_ROOT / "fii_dii" / f"fii_dii_{day.replace('-', '')}.csv"
            _archive_file(ds, day, target, csv_payload, len(frame), list(frame.columns), url)
    except Exception as exc:
        logging.warning("FII/DII snapshot: %s", exc)
        # Keep prior verified dates. Do not create zero-valued placeholder rows.


def collect(lookback_calendar_days=60, end_date=None, pause_seconds=0.25, session_factory=requests.Session):
    if not 1 <= lookback_calendar_days <= 366:
        raise ValueError("lookback_calendar_days must be between 1 and 366.")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    session = session_factory()
    session.headers.update(HEADERS)
    end = end_date or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    days = [end - timedelta(days=i) for i in range(lookback_calendar_days)
            if (end - timedelta(days=i)).weekday() < 5]
    for day in reversed(days):
        _collect_date_reports(session, day, manifest, lambda: time.sleep(pause_seconds))
        _collect_indices(session, day, manifest, lambda: time.sleep(pause_seconds))
    _collect_fii_dii(session, manifest)
    checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for kind in DATASETS:
        ds = manifest["datasets"].setdefault(kind, {"label": DATASETS[kind], "records": {}})
        available = sorted(d for d, r in ds.get("records", {}).items() if r.get("status") == "available")
        ds["latest_available_date"] = available[-1] if available else None
        ds["available_dates"] = available
        ds["latest_20_sessions"] = available[-20:]
    manifest["updated_at_utc"] = checked
    manifest["core_collector_lookback_calendar_days"] = lookback_calendar_days
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-calendar-days", type=int, default=60)
    parser.add_argument("--end-date")
    args = parser.parse_args()
    result = collect(args.lookback_calendar_days,
                     date.fromisoformat(args.end_date) if args.end_date else None)
    for kind in DATASETS:
        ds = result["datasets"][kind]
        logging.info("%s latest=%s available=%d", kind, ds.get("latest_available_date"),
                     len(ds.get("available_dates", [])))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
