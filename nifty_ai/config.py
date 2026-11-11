from pathlib import Path
import os

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("NIFTY_DATA_DIR", str(ROOT / "var"))).expanduser().resolve()
INCOMING_DIR = Path(os.getenv("NIFTY_INCOMING_DIR", str(ROOT / "INCOMING_REPORTS"))).expanduser().resolve()
RAW_DIR = DATA_DIR / "raw"
PROCESSED_DIR = DATA_DIR / "processed"
DB_PATH = DATA_DIR / "nifty_ai.sqlite3"
APP_PASSWORD = os.getenv("NIFTY_APP_PASSWORD", "")
ENABLE_WATCHER = os.getenv("NIFTY_ENABLE_WATCHER", "true").lower() in {"1", "true", "yes", "on"}
MAX_UPLOAD_MB = int(os.getenv("NIFTY_MAX_UPLOAD_MB", "100"))

for directory in (DATA_DIR, INCOMING_DIR, RAW_DIR, PROCESSED_DIR):
    directory.mkdir(parents=True, exist_ok=True)
