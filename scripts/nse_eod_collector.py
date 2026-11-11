"""Download and archive NSE participant volume and FII derivatives EOD reports.

Only validated official NSE files are written. Missing reports are recorded as
not_available; the collector never fabricates or substitutes market data.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Callable
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
DATA_ROOT = ROOT / "data" / "nse_reports"
MANIFEST_PATH = DATA_ROOT / "manifest.json"
NSE_HOME = "https://www.nseindia.com/"
NSE_REPORTS = "https://www.nseindia.com/all-reports-derivatives"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": NSE_REPORTS,
    "Connection": "keep-alive",
}
REPORTS = {
    "participant_volume": {
        "label": "F&O Participant-wise Trading Volumes",
        "directory": "participant_volume",
        "url": "https://nsearchives.nseindia.com/content/nsccl/fao_participant_vol_{date}.csv",
        "filename": "fao_participant_vol_{date}.csv",
    },
    "fii_derivatives": {
        "label": "F&O FII Derivatives Statistics",
        "directory": "fii_derivatives",
        "url": "https://nsearchives.nseindia.com/content/fo/fii_stats_{date}.xls",
        "filename": "fii_stats_{date}.xls",
    },
}


def _parse_report(kind: str, payload: bytes) -> tuple[int, list[str]]:
    if not payload or len(payload) < 32:
        raise ValueError("Downloaded file is empty or too small")
    if kind == "participant_volume":
        last_error = None
        for encoding in ("utf-8-sig", "latin1"):
            try:
                frame = pd.read_csv(BytesIO(payload), sep=None, engine="python", encoding=encoding)
                frame = frame.dropna(how="all")
                if frame.empty or len(frame.columns) < 2:
                    raise ValueError("CSV has no usable tabular rows")
                return int(len(frame)), [str(c).strip() for c in frame.columns]
            except Exception as exc:
                last_error = exc
        raise ValueError(f"Participant volume CSV validation failed: {last_error}")

    # NSE publishes this report as legacy Excel .xls. Refuse HTML/CSV error pages.
    if not payload.startswith(bytes.fromhex("D0CF11E0A1B11AE1")):
        raise ValueError("FII report is not a genuine XLS/OLE workbook")
    book = pd.read_excel(BytesIO(payload), sheet_name=None, engine="xlrd")
    frames = [frame.dropna(how="all") for frame in book.values() if isinstance(frame, pd.DataFrame)]
    frames = [frame for frame in frames if not frame.empty]
    if not frames:
        raise ValueError("FII XLS contains no readable rows")
    columns = list(dict.fromkeys(str(c).strip() for frame in frames for c in frame.columns))
    return sum(len(frame) for frame in frames), columns


def _session() -> requests.Session:
    session = requests.Session()
    session.headers.update(HEADERS)
    # NSE archive hosts may require a first-party session cookie.
    session.get(NSE_HOME, timeout=(10, 25)).raise_for_status()
    session.get(NSE_REPORTS, timeout=(10, 25)).raise_for_status()
    return session


def _download(session: requests.Session, url: str, attempts: int = 3,
              sleep: Callable[[float], None] = time.sleep) -> bytes | None:
    last_error = None
    for attempt in range(attempts):
        try:
            response = session.get(url, timeout=(10, 40))
            if response.status_code == 404:
                return None
            response.raise_for_status()
            body = response.content
            if not body:
                raise ValueError("NSE returned an empty response")
            return body
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt + 1 < attempts:
                sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Download failed after {attempts} attempts: {last_error}")


def _report_date(kind: str, day: date) -> str:
    return day.strftime("%d%m%Y") if kind == "participant_volume" else day.strftime("%d-%b-%Y")


def _display_path(target: Path) -> str:
    try:
        return str(target.relative_to(ROOT)).replace(os.sep, "/")
    except ValueError:
        return str(target)


def _load_manifest() -> dict:
    if not MANIFEST_PATH.exists():
        return {"schema_version": 1, "source": "NSE official public EOD archives", "datasets": {}}
    try:
        value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        if value.get("schema_version") != 1 or not isinstance(value.get("datasets"), dict):
            raise ValueError("Unexpected manifest schema")
        return value
    except Exception as exc:
        raise RuntimeError(f"Existing collector manifest is invalid; refusing to overwrite: {exc}") from exc


def collect(lookback_calendar_days: int = 60, end_date: date | None = None,
            session_factory: Callable[[], requests.Session] = _session,
            sleep: Callable[[float], None] = time.sleep) -> dict:
    if lookback_calendar_days < 1 or lookback_calendar_days > 366:
        raise ValueError("lookback_calendar_days must be between 1 and 366")
    DATA_ROOT.mkdir(parents=True, exist_ok=True)
    manifest = _load_manifest()
    session = session_factory()
    end = end_date or datetime.now(ZoneInfo("Asia/Kolkata")).date()
    days = [end - timedelta(days=i) for i in range(lookback_calendar_days)
            if (end - timedelta(days=i)).weekday() < 5]
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    for kind, spec in REPORTS.items():
        dataset = manifest["datasets"].setdefault(kind, {"label": spec["label"], "records": {}})
        records = dataset.setdefault("records", {})
        for day in reversed(days):
            iso_day = day.isoformat()
            date_token = _report_date(kind, day)
            filename = spec["filename"].format(date=date_token)
            target_dir = DATA_ROOT / spec["directory"]
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / filename
            existing = records.get(iso_day)
            if target.exists():
                try:
                    payload = target.read_bytes()
                    rows, columns = _parse_report(kind, payload)
                    records[iso_day] = {
                        "status": "available", "filename": _display_path(target),
                        "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
                        "rows": rows, "columns": columns[:60],
                        "source_url": spec["url"].format(date=date_token),
                        "last_checked_utc": checked_at,
                    }
                    continue
                except Exception:
                    target.unlink(missing_ok=True)
            url = spec["url"].format(date=date_token)
            try:
                payload = _download(session, url, attempts=3, sleep=sleep)
                if payload is None:
                    records[iso_day] = {
                        "status": "not_available", "source_url": url,
                        "last_checked_utc": checked_at,
                    }
                    continue
                rows, columns = _parse_report(kind, payload)
                temp = target.with_suffix(target.suffix + ".tmp")
                temp.write_bytes(payload)
                temp.replace(target)
                records[iso_day] = {
                    "status": "available", "filename": _display_path(target),
                    "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload),
                    "rows": rows, "columns": columns[:60], "source_url": url,
                    "last_checked_utc": checked_at,
                }
            except Exception as exc:
                # Never overwrite a verified local archive with a transient fetch failure.
                if not (existing and existing.get("status") == "available" and target.exists()):
                    records[iso_day] = {
                        "status": "error", "source_url": url,
                        "error": str(exc)[:500], "last_checked_utc": checked_at,
                    }
                logging.warning("%s %s: %s", kind, iso_day, exc)
                sleep(0.25)

        available = sorted(d for d, record in records.items() if record.get("status") == "available")
        dataset["latest_available_date"] = available[-1] if available else None
        dataset["available_dates"] = available
        dataset["latest_20_sessions"] = available[-20:]

    manifest["updated_at_utc"] = checked_at
    manifest["lookback_calendar_days"] = lookback_calendar_days
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lookback-calendar-days", type=int, default=60)
    parser.add_argument("--end-date", help="Optional ISO date for reproducible backfills")
    args = parser.parse_args()
    end_date = date.fromisoformat(args.end_date) if args.end_date else None
    manifest = collect(args.lookback_calendar_days, end_date=end_date)
    for kind, data in manifest["datasets"].items():
        logging.info("%s latest=%s available_dates=%s", kind,
                     data.get("latest_available_date"), len(data.get("available_dates", [])))
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    raise SystemExit(main())
