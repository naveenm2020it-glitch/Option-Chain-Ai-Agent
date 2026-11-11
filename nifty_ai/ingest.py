import hashlib
import time
from pathlib import Path
from .config import INCOMING_DIR, RAW_DIR
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


def import_path(path: Path, trading_date=None):
    if not path.exists() or not path.is_file() or path.suffix.lower() not in ALLOWED:
        return None
    if not _wait_until_stable(path):
        return None
    content = path.read_bytes()
    fp = fingerprint(content)
    report_type = identify_report(path.name)
    info = inspect_report(report_type, str(path))
    date = info.get("trading_date") or trading_date
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


def scan_incoming(trading_date=None):
    added = []
    if not INCOMING_DIR.exists():
        return added
    for path in INCOMING_DIR.rglob("*"):
        if path.is_file() and path.suffix.lower() in ALLOWED:
            result = import_path(path, trading_date=trading_date)
            if result:
                added.append(result)
    return added


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
