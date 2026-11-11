import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path
from .config import INCOMING_DIR, RAW_DIR, ROOT
from .database import add_report, update_report_status
from .reports import identify_report, inspect_report, fingerprint

ALLOWED = {".csv", ".zip", ".xls", ".xlsx", ".dat", ".txt"}


def _wait_until_stable(path: Path, attempts=8):
    previous = -1
    stable = 0
    for _ in range(attempts):
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if size > 0 and size == previous:
            stable += 1
            if stable >= 2:
                return True
        else:
            stable = 0
        previous = size
        time.sleep(0.35)
    return path.exists() and path.stat().st_size > 0


def _date_from_filename(filename: str):
    name = Path(filename).name
    match = re.search(r"fao_participant_vol_(\d{2})(\d{2})(\d{4})", name, re.I)
    if match:
        try:
            return datetime.strptime("".join(match.groups()), "%d%m%Y").date().isoformat()
        except ValueError:
            return None
    match = re.search(r"fii_stats_(\d{2}-[A-Za-z]{3}-\d{4})", name, re.I)
    if match:
        try:
            return datetime.strptime(match.group(1), "%d-%b-%Y").date().isoformat()
        except ValueError:
            return None
    return None


def import_path(path: Path, trading_date=None, wait_stable=True):
    if not path.exists() or not path.is_file() or path.suffix.lower() not in ALLOWED:
        return None
    if wait_stable and not _wait_until_stable(path):
        return None
    content = path.read_bytes()
    fp = fingerprint(content)
    report_type = identify_report(path.name)
    info = inspect_report(report_type, str(path))
    report_type = info.get("report_type", report_type)
    date = info.get("trading_date") or trading_date or _date_from_filename(path.name)
    item = {
        "fingerprint": fp,
        "original_name": path.name,
        "report_type": report_type,
        "trading_date": date,
        "file_path": str(path.resolve()),
        "byte_size": len(content),
        "row_count": info.get("row_count", 0),
        "columns": info.get("columns", []),
        "warnings": info.get("warnings", []),
        "status": "imported" if info.get("row_count", 0) > 0 else "unreadable",
    }
    return add_report(item)


def scan_directory(directory: Path, trading_date=None, wait_stable=True):
    added = []
    directory = Path(directory)
    if not directory.exists():
        return added
    for path in directory.rglob("*"):
        if path.is_file() and path.suffix.lower() in ALLOWED:
            result = import_path(path, trading_date=trading_date, wait_stable=wait_stable)
            if result:
                added.append(result)
    return added


def scan_incoming(trading_date=None):
    return scan_directory(INCOMING_DIR, trading_date=trading_date)


def store_upload(filename: str, content: bytes, trading_date=None):
    safe_name = Path(filename.replace("\\", "/")).name
    if not safe_name or Path(safe_name).suffix.lower() not in ALLOWED:
        raise ValueError("Unsupported file. Upload CSV, ZIP, XLS, XLSX, DAT or TXT reports.")
    day = trading_date or "undated"
    target_dir = INCOMING_DIR / day
    target_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()[:12]
    destination = target_dir / f"{digest}_{safe_name}"
    if not destination.exists():
        destination.write_bytes(content)
    result = import_path(destination, trading_date=trading_date)
    return result



def scan_manifest(directory: Path):
    """Import report metadata from the collector manifest without reparsing archives.

    The collector has already validated each official file and stored its SHA,
    row count, and columns. This keeps Render startup quick while preserving
    historical report metadata and the exact original files.
    """
    directory = Path(directory)
    manifest_path = directory / "manifest.json"
    if not manifest_path.is_file():
        return scan_directory(directory, wait_stable=False)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1 or not isinstance(manifest.get("datasets"), dict):
        raise ValueError("NSE EOD manifest has an unsupported schema.")
    added = []
    for report_type, dataset in manifest["datasets"].items():
        for trading_date, record in dataset.get("records", {}).items():
            if record.get("status") != "available" or not record.get("filename"):
                continue
            path = (ROOT / record["filename"]).resolve()
            try:
                path.relative_to(ROOT.resolve())
            except ValueError:
                continue
            if not path.is_file():
                continue
            digest = record.get("sha256") or hashlib.sha256(path.read_bytes()).hexdigest()
            item = {
                "fingerprint": digest,
                "original_name": path.name,
                "report_type": report_type,
                "trading_date": trading_date,
                "file_path": str(path),
                "byte_size": record.get("bytes") or path.stat().st_size,
                "row_count": record.get("rows", 0),
                "columns": record.get("columns", []),
                "warnings": [],
                "status": "imported" if record.get("rows", 0) > 0 else "unreadable",
            }
            added.append(add_report(item))
    return added
