from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from data_quality import assess_manifest
from storage import history_catalog, b2_configured, read_collection_manifest, archive_download_url

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="NIFTY 50 Next-Day Market Intelligence", version="0.2.0",
              description="Evidence-led research dashboard. Not investment advice.")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
def home():
    return HTMLResponse((ROOT / "static" / "index.html").read_text(encoding="utf-8"))


@app.get("/api/health")
def health():
    return {"status": "ok", "app": "nifty50-next-day-intelligence", "version": app.version,
            "utc_time": datetime.now(timezone.utc).isoformat()}


@app.get("/api/status")
def status():
    report_root = DATA_DIR / "nse_reports"
    files = list(report_root.rglob("*")) if report_root.exists() else []
    manifest_path = report_root / "manifest.json"
    manifest = None
    manifest_error = None
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            manifest_error = f"Cannot read NSE manifest: {exc}"

    quality = assess_manifest(manifest)
    if manifest_error:
        quality["status"] = "blocked"
        quality["aligned"] = False
        quality["issues"].append(manifest_error)

    ready = quality["aligned"]
    return {
        "market": "NIFTY 50",
        "options_scope": "NIFTY only",
        "data_mode": "validated_eod" if ready else "blocked_data_quality",
        "report_files": sum(p.is_file() for p in files),
        "data_quality": quality,
        "historical_storage": "backblaze_b2_configured" if b2_configured() else "not_configured",
        "prediction": None,
        "message": ("Core NSE reports are aligned. Forecast engine is not enabled yet."
                    if ready else
                    "Prediction blocked: core NSE reports must be present and from one trading session."),
    }


@app.get("/api/history")
def history():
    if not b2_configured():
        return {"configured": False, "storage": "backblaze_b2", "collections": [],
                "message": "Historical archive storage is not configured yet."}
    return {"configured": True, "storage": "backblaze_b2", **history_catalog()}

@app.get("/api/history/{collection_date}")
def history_manifest(collection_date: str):
    """Return report-level dates and metadata for one archived collection."""
    if not b2_configured():
        raise HTTPException(status_code=503, detail="Backblaze B2 is not configured.")
    try:
        return {"collection_date": collection_date,
                "manifest": read_collection_manifest(collection_date)}
    except ValueError:
        raise HTTPException(status_code=400, detail="Date must use YYYY-MM-DD.")
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code in {"NoSuchKey", "404", "NotFound"}:
            raise HTTPException(status_code=404, detail="Archived collection not found.")
        raise HTTPException(status_code=502, detail="Could not read archive manifest from B2.")


@app.get("/api/history/{collection_date}/download")
def history_download(collection_date: str):
    """Return a 15-minute signed download URL; bucket remains private."""
    if not b2_configured():
        raise HTTPException(status_code=503, detail="Backblaze B2 is not configured.")
    try:
        return {"collection_date": collection_date,
                "expires_in_seconds": 900,
                "download_url": archive_download_url(collection_date)}
    except ValueError:
        raise HTTPException(status_code=400, detail="Date must use YYYY-MM-DD.")
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code in {"NoSuchKey", "404", "NotFound"}:
            raise HTTPException(status_code=404, detail="Archived collection not found.")
        raise HTTPException(status_code=502, detail="Could not create archive download link.")
