from __future__ import annotations
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

import secrets

import database
from data_quality import assess_manifest
from seller_engine import analyze_seller_market
from storage import history_catalog, b2_configured, read_catalog, read_collection_manifest, archive_download_url

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
app = FastAPI(title="NIFTY 50 Next-Day Market Intelligence", version="0.3.0",
              description="Seller-first evidence-led research dashboard. Not investment advice.")
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
        "seller_engine": {"implemented": True, "models": 8, "live_data_connected": False},
        "message": ("Core NSE reports are aligned. Seller analysis code is implemented; "
                    "raw NSE report adapters and live forecast are not enabled yet."
                    if ready else
                    "Prediction blocked: core NSE reports must be present and from one trading session."),
    }


@app.post("/api/engine/analyze")
def engine_analyze(payload: dict):
    """Run seller ensemble on a validated, normalized snapshot; not raw NSE files."""
    try:
        return analyze_seller_market(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/engine/models")
def engine_models():
    return {
        "orientation": "seller_first",
        "parallel_subprocesses": [
            "CE seller", "PE seller", "OI/PCR", "seller strength",
            "futures confirmation", "participant positioning",
            "volatility risk", "seller trap risk",
        ],
        "master_engine": "weighted consensus with data-quality, disagreement and risk gates",
        "live_data_connected": False,
    }


@app.get("/api/history")
def history():
    if database.database_configured():
        try:
            catalog = history_catalog() if b2_configured() else {
                "schema_version": 1, "updated_at_utc": None,
                "collections": database.list_daily_reports()
            }
            return {"configured": True, "storage": "postgresql", **catalog}
        except Exception:
            # If PostgreSQL is unavailable, retain the B2 fallback where possible.
            if b2_configured():
                try:
                    return {"configured": True, "storage": "backblaze_b2",
                            **read_catalog()}
                except Exception:
                    pass
            raise HTTPException(status_code=503, detail="Historical metadata stores are temporarily unavailable.")
    if not b2_configured():
        return {"configured": False, "storage": "backblaze_b2", "collections": [],
                "message": "Historical archive storage is not configured yet."}
    return {"configured": True, "storage": "backblaze_b2", **history_catalog()}


@app.post("/api/internal/daily-report")
def ingest_daily_report(payload: dict, authorization: str | None = Header(default=None)):
    """Store the collector's daily NSE manifest in PostgreSQL; never expose this endpoint publicly."""
    token = os.getenv("DAILY_INGEST_TOKEN")
    if not token:
        raise HTTPException(status_code=503, detail="Daily report ingestion is not configured.")
    supplied = authorization.removeprefix("Bearer ").strip() if authorization else ""
    if not secrets.compare_digest(supplied, token):
        raise HTTPException(status_code=401, detail="Unauthorized.")
    collection_date = payload.get("collection_date")
    manifest = payload.get("manifest")
    if not isinstance(collection_date, str) or not isinstance(manifest, dict):
        raise HTTPException(status_code=400, detail="collection_date and manifest are required.")
    if not database.database_configured():
        raise HTTPException(status_code=503, detail="Database persistence is not configured.")
    try:
        result = database.upsert_daily_report(collection_date, manifest)
        return {"stored": True, **result}
    except ValueError:
        raise HTTPException(status_code=400, detail="collection_date must use YYYY-MM-DD.")
    except Exception:
        raise HTTPException(status_code=503, detail="Could not persist daily report metadata.")

@app.get("/api/history/{collection_date}")
def history_manifest(collection_date: str):
    """Return report-level dates and metadata for one archived collection."""
    try:
        stored_manifest = database.get_daily_report_manifest(collection_date)
        if stored_manifest is not None:
            return {"collection_date": collection_date, "manifest": stored_manifest,
                    "storage": "postgresql"}
        if not b2_configured():
            raise HTTPException(status_code=503, detail="No stored manifest is available.")
        return {"collection_date": collection_date,
                "manifest": read_collection_manifest(collection_date), "storage": "backblaze_b2"}
    except ValueError:
        raise HTTPException(status_code=400, detail="Date must use YYYY-MM-DD.")
    except HTTPException:
        raise
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


@app.get("/api/performance")
def performance(from_date: str | None = None, to_date: str | None = None):
    """Prediction outcome feed placeholder; populated after daily forecasts are persisted and evaluated."""
    return {
        "available": False,
        "records": [],
        "message": "Prediction outcomes are not being stored/evaluated yet. The heatmap is ready; green/red results will appear after daily predictions are recorded and compared with the next session's actual NIFTY outcome.",
    }
