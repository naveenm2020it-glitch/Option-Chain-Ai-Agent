"""Collect the five NSE EOD report types that were previously manual-only.

For the latest NSE sessions, discover official file names and paths from NSE's
daily-reports APIs. For historical backfill, use documented NSE archive paths.
Every downloaded file is validated, hashed and recorded; failures remain
explicit and are never replaced with stale or fabricated data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import time
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data" / "nse_reports"
MANIFEST_PATH = DATA_ROOT / "manifest.json"
NSE = "https://www.nseindia.com"
ARCHIVE = "https://nsearchives.nseindia.com"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json,text/csv,application/vnd.ms-excel,*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": f"{NSE}/all-reports-derivatives",
    "Connection": "keep-alive",
}

# NSE daily-report API display names are matched conservatively. Archive URLs
# are documented fallbacks for older dates and temporary API discovery failure.
SPECS = {
    "cm_delivery": {
        "label": "CM Security-wise Delivery Positions",
        "segment": "CM", "display": ("security-wise delivery positions",),
        "archive": "archives/equities/mto/MTO_{d}.DAT",
        "filename": "MTO_{d}.DAT", "format": "dat",
    },
    "cm_volatility": {
        "label": "CM Daily Volatility",
        "segment": "CM", "display": ("daily volatility",),
        "archive": "archives/nsccl/volt/CMVOLT_{d}.CSV",
        "filename": "CMVOLT_{d}.CSV", "format": "csv",
    },
    "participant_oi": {
        "label": "F&O Participant-wise Open Interest",
        "segment": "FO", "display": ("participant wise open interest", "participant-wise open interest"),
        "archive": "content/nsccl/fao_participant_oi_{d}.csv",
        "filename": "fao_participant_oi_{d}.csv", "format": "csv",
    },
    "fo_settlement": {
        "label": "F&O Daily Settlement Prices",
        "segment": "FO", "display": ("daily settlement prices",),
        "archive": "archives/nsccl/sett/FOSett_prce_{d}.csv",
        "filename": "FOSett_prce_{d}.csv", "format": "csv",
    },
    "fo_volatility": {
        "label": "F&O Daily Volatility",
        "segment": "FO", "display": ("daily volatility",),
        "archive": "archives/nsccl/volt/FOVOLT_{d}.csv",
        "filename": "FOVOLT_{d}.csv", "format": "csv",
    },
}

def _load_manifest():
    if not MANIFEST_PATH.exists():
        return {"schema_version": 1, "source": "NSE official public EOD archives", "datasets": {}}
    data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1 or not isinstance(data.get("datasets"), dict):
        raise RuntimeError("Existing NSE manifest has an unsupported schema; refusing overwrite.")
    return data

def _date_key(value):
    parsed = pd.to_datetime(value, errors="coerce", dayfirst=True)
    return None if pd.isna(parsed) else parsed.date().isoformat()

def _discover(session, segment):
    url = f"{NSE}/api/daily-reports?key={segment}"
    response = session.get(url, timeout=(10, 30))
    response.raise_for_status()
    payload = response.json()
    found = {}
    for bucket in ("CurrentDay", "PreviousDay"):
        for row in payload.get(bucket, []) or []:
            display = str(row.get("displayName", "")).lower()
            trade_date = _date_key(row.get("tradingDate"))
            filename = str(row.get("fileActlName", "")).strip()
            base = str(row.get("filePath", "")).strip()
            if trade_date and filename and base.startswith("https://nsearchives.nseindia.com/"):
                found.setdefault(trade_date, []).append({
                    "display": display, "url": base.rstrip("/") + "/" + filename,
                    "filename": filename, "api": url,
                })
    return found

def _match_discovered(kind, day, discovered):
    spec = SPECS[kind]
    candidates = discovered.get(day.isoformat(), [])
    for item in candidates:
        display_key = "".join(ch for ch in str(item.get("display", "")).lower() if ch.isalnum())
        if any("".join(ch for ch in token.lower() if ch.isalnum()) in display_key
               for token in spec["display"]):
            # The CM and FO APIs both publish "Daily Volatility". The API
            # segment disambiguates CMVOLT from FOVOLT.
            name = item["filename"].upper()
            if kind == "cm_volatility" and not name.startswith("CMVOLT_"):
                continue
            if kind == "fo_volatility" and not name.startswith("FOVOLT_"):
                continue
            return item["url"], item["filename"], item["api"]
    return None

def _validate(kind, payload):
    if not payload or len(payload) < 32:
        raise ValueError("Empty or undersized response")
    if payload.lstrip().lower().startswith((b"<!doctype html", b"<html")):
        raise ValueError("Received an HTML block/error page, not a report")
    fmt = SPECS[kind]["format"]
    if fmt == "dat":
        # MTO is a text data file; reject binary or obvious HTML responses.
        try:
            text = payload.decode("latin1")
        except Exception as exc:
            raise ValueError(f"MTO text decoding failed: {exc}") from exc
        lines = [line for line in text.splitlines() if line.strip()]
        if len(lines) < 2:
            raise ValueError("MTO file has no usable tabular rows")
        return len(lines) - 1, [lines[0][:200]]
    last = None
    for encoding in ("utf-8-sig", "latin1"):
        try:
            frame = pd.read_csv(BytesIO(payload), sep=None, engine="python", encoding=encoding)
            frame = frame.dropna(how="all")
            cols = [str(c).strip() for c in frame.columns]
            if frame.empty or len(cols) < 2:
                raise ValueError("CSV contains no usable table")
            # Basic content checks prevent HTML/error payloads parsed as CSV.
            if not any(any(ch.isalpha() for ch in col) for col in cols):
                raise ValueError("CSV header does not contain readable field names")
            return int(len(frame)), cols
        except Exception as exc:
            last = exc
    raise ValueError(f"CSV validation failed: {last}")

def _download(session, url, attempts=3):
    last = None
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=(10, 45))
            if response.status_code == 404:
                return None
            response.raise_for_status()
            if not response.content:
                raise ValueError("Empty HTTP response")
            return response.content
        except (requests.RequestException, ValueError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Download failed after {attempts} attempts: {last}")

def collect(lookback_calendar_days=60, end_date=None, session_factory=requests.Session,
            sleep=time.sleep):
    if not 1 <= lookback_calendar_days <= 366:
        raise ValueError("lookback_calendar_days must be between 1 and 366")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    session = session_factory()
    session.headers.update(HEADERS)
    discovered = {}
    for segment in ("CM", "FO"):
        try:
            discovered[segment] = _discover(session, segment)
        except Exception as exc:
            logging.warning("NSE %s report discovery failed: %s", segment, exc)
            discovered[segment] = {}

    end = end_date or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    days = [end - timedelta(days=i) for i in range(lookback_calendar_days)
            if (end - timedelta(days=i)).weekday() < 5]
    checked = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for kind, spec in SPECS.items():
        dataset = manifest["datasets"].setdefault(kind, {"label": spec["label"], "records": {}})
        records = dataset.setdefault("records", {})
        for day in reversed(days):
            iso = day.isoformat()
            token = day.strftime("%d%m%Y")
            existing = records.get(iso, {})
            found = _match_discovered(kind, day, discovered.get(spec["segment"], {}))
            if found:
                url, filename, discovery_url = found
            else:
                filename = spec["filename"].format(d=token)
                url = f"{ARCHIVE}/{spec['archive'].format(d=token)}"
                discovery_url = None

            target = DATA_ROOT / kind / filename
            if target.exists():
                try:
                    payload = target.read_bytes()
                    rows, columns = _validate(kind, payload)
                    records[iso] = {
                        "status": "available", "filename": target.relative_to(ROOT).as_posix(),
                        "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
                        "rows": rows, "columns": columns[:60], "source_url": url,
                        "last_checked_utc": checked,
                    }
                    continue
                except Exception:
                    target.unlink(missing_ok=True)

            try:
                payload = _download(session, url)
                if payload is None:
                    # A 404 from a stale filename can occur during publication.
                    # Keep it explicitly missing; next scheduled run retries it.
                    records[iso] = {"status": "not_available", "source_url": url,
                                    "discovery_url": discovery_url, "last_checked_utc": checked}
                    continue
                rows, columns = _validate(kind, payload)
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_suffix(target.suffix + ".tmp")
                temp.write_bytes(payload)
                temp.replace(target)
                records[iso] = {
                    "status": "available", "filename": target.relative_to(ROOT).as_posix(),
                    "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
                    "rows": rows, "columns": columns[:60], "source_url": url,
                    "discovery_url": discovery_url, "last_checked_utc": checked,
                }
            except Exception as exc:
                if not (existing.get("status") == "available" and target.exists()):
                    records[iso] = {"status": "error", "source_url": url,
                                    "discovery_url": discovery_url,
                                    "error": str(exc)[:500], "last_checked_utc": checked}
                logging.warning("%s %s: %s", kind, iso, exc)
            sleep(0.2)

        available = sorted(d for d, rec in records.items() if rec.get("status") == "available")
        dataset["latest_available_date"] = available[-1] if available else None
        dataset["available_dates"] = available
        dataset["latest_20_sessions"] = available[-20:]

    manifest["updated_at_utc"] = checked
    manifest["extended_collector_lookback_calendar_days"] = lookback_calendar_days
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-calendar-days", type=int, default=60)
    parser.add_argument("--end-date")
    args = parser.parse_args()
    result = collect(args.lookback_calendar_days,
                     date.fromisoformat(args.end_date) if args.end_date else None)
    for kind in SPECS:
        ds = result["datasets"][kind]
        logging.info("%s latest=%s available=%d", kind, ds.get("latest_available_date"),
                     len(ds.get("available_dates", [])))

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
