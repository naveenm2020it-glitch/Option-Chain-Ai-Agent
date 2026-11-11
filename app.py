from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
import secrets
import os
from fastapi import FastAPI, UploadFile, File, Form, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from nifty_ai import database
from nifty_ai.config import ROOT, INCOMING_DIR, DATA_DIR, APP_PASSWORD, ENABLE_WATCHER, MAX_UPLOAD_MB
from nifty_ai.ingest import scan_incoming, scan_directory, store_upload
from nifty_ai.analytics import run_analysis
from nifty_ai.reports import REPORTS
from nifty_ai.watcher import FolderWatcher

watcher = FolderWatcher()
AUTO_REPORT_DIR = ROOT / "data" / "nse_reports"


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    scan_incoming()
    # The GitHub Actions collector commits verified NSE EOD files here. Import
    # them on every process start so the analysis can use them without uploads.
    scan_directory(AUTO_REPORT_DIR, wait_stable=False)
    if ENABLE_WATCHER and not os.getenv("RENDER"):
        watcher.start()
    yield
    watcher.stop()


app = FastAPI(title="NIFTY Next-Day AI Intelligence", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")


def _auth(password: Optional[str]):
    if APP_PASSWORD and not (password and secrets.compare_digest(password, APP_PASSWORD)):
        raise HTTPException(status_code=401, detail="Authentication required")


@app.get("/")
def home():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/health")
def health():
    return {"ok": True, "app": "NIFTY Next-Day AI Intelligence", "version": app.version,
            "auth_required": bool(APP_PASSWORD), "watcher_enabled": watcher.enabled,
            "storage_mode": "render-ephemeral" if os.getenv("RENDER") else "local"}


@app.post("/api/auth")
async def authenticate(request: Request):
    payload = await request.json()
    password = str(payload.get("password", ""))
    if APP_PASSWORD and not secrets.compare_digest(password, APP_PASSWORD):
        raise HTTPException(status_code=401, detail="Incorrect access password")
    return {"ok": True, "authenticated": True}


@app.get("/api/state")
def state(x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    reports = database.list_reports()
    analyses = database.list_analyses(1)
    counts = {}
    for key in REPORTS:
        counts[key] = sum(1 for r in reports if r["report_type"] == key)
    recent = analyses[0] if analyses else None
    return {"reports": reports[:80], "report_counts": counts, "report_catalog": REPORTS,
            "latest_analysis": recent, "historical_sessions": len(database.get_session_prices(5000)),
            "incoming_folder": str(INCOMING_DIR), "watcher_enabled": watcher.enabled,
            "storage_mode": "render-ephemeral" if os.getenv("RENDER") else "local"}


@app.get("/api/reports")
def get_reports(trading_date: Optional[str] = None, x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    return {"items": database.list_reports(trading_date)}


@app.post("/api/import")
async def import_reports(files: list[UploadFile] = File(...), trading_date: Optional[str] = Form(default=None),
                         x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    if not files:
        raise HTTPException(status_code=400, detail="Select at least one report file.")
    imported, errors = [], []
    max_bytes = MAX_UPLOAD_MB * 1024 * 1024
    for upload in files:
        content = await upload.read()
        if len(content) > max_bytes:
            errors.append({"file": upload.filename, "error": f"File exceeds {MAX_UPLOAD_MB} MB limit."})
            continue
        try:
            item = store_upload(upload.filename or "report.csv", content, trading_date)
            imported.append(item)
        except Exception as exc:
            errors.append({"file": upload.filename, "error": str(exc)})
    return {"imported": imported, "errors": errors, "count": len(imported)}


@app.post("/api/scan")
def scan(x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    added = scan_incoming()
    return {"added": added, "count": len(added), "folder": str(INCOMING_DIR)}


@app.post("/api/analyze")
def analyze(trading_date: Optional[str] = Form(default=None), x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    all_reports = database.list_reports(trading_date)
    if not all_reports:
        raise HTTPException(status_code=400, detail="No reports found for this date. Import the NSE files first.")
    dates = [r["trading_date"] for r in all_reports if r.get("trading_date")]
    effective_date = trading_date or (max(dates) if dates else None)
    if not effective_date:
        raise HTTPException(status_code=400, detail="Trading date is unknown. Re-import files after choosing their trading date.")
    historical_reports = database.list_reports()
    result = run_analysis(effective_date, all_reports, history_reports=historical_reports)
    status = "complete" if result["data_completeness"] >= 75 else "partial"
    run_id = database.save_analysis(effective_date, result, status)
    return {"run_id": run_id, "status": status, "result": result}


@app.get("/api/analyses")
def analyses(x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    return {"items": database.list_analyses(50)}


@app.get("/api/prices")
def prices(limit: int = 180, x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    return {"items": database.get_session_prices(max(1, min(limit, 2000)))}


@app.get("/api/config")
def config(x_app_password: Optional[str] = Header(default=None)):
    _auth(x_app_password)
    return {"incoming_folder": str(INCOMING_DIR), "data_folder": str(DATA_DIR), "watcher_enabled": watcher.enabled,
            "render_mode": bool(os.getenv("RENDER")), "max_upload_mb": MAX_UPLOAD_MB}
