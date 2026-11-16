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
from seller_levels import build_seller_levels
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

    data_source = "local_filesystem"
    latest_collection_date = None
    if manifest is None and not manifest_error and b2_configured():
        # The scheduled collector runs in GitHub Actions, not on this Render
        # filesystem. Read the latest durable B2 manifest so the hosted API
        # reports the actual collected data instead of always showing empty local state.
        try:
            catalog = history_catalog()
            collections = catalog.get("collections", [])
            valid_dates = [
                item.get("collection_date")
                for item in collections
                if isinstance(item, dict) and item.get("collection_date")
            ]
            if valid_dates:
                latest_collection_date = max(valid_dates)
                manifest = read_collection_manifest(latest_collection_date)
                data_source = "backblaze_b2"
        except Exception as exc:
            manifest_error = f"Could not read the latest B2 collection manifest: {type(exc).__name__}"

    quality = assess_manifest(manifest)
    if manifest_error:
        quality["status"] = "blocked"
        quality["aligned"] = False
        quality["issues"].append(manifest_error)

    manifest_results = manifest.get("results", {}) if isinstance(manifest, dict) else {}
    manifest_file_count = sum(
        1 for item in manifest_results.values()
        if isinstance(item, dict) and item.get("file")
    ) if isinstance(manifest_results, dict) else 0
    ready = quality["aligned"]
    return {
        "market": "NIFTY 50",
        "options_scope": "NIFTY only",
        "data_mode": "validated_eod" if ready else "blocked_data_quality",
        "data_source": data_source,
        "latest_collection_date": latest_collection_date,
        "report_files": max(sum(p.is_file() for p in files), manifest_file_count),
        "data_quality": quality,
        "historical_storage": "backblaze_b2_configured" if b2_configured() else "not_configured",
        "prediction": None,
        "seller_engine": {"implemented": True, "models": 8, "live_data_connected": False,
                          "seller_levels_engine": True, "levels_live_data_connected": False,
                          "daily_eod_adapter": True, "seller_context_windows": [5, 22],
                          "seller_context_persistence": "postgresql"},
        "message": ("Core NSE reports are aligned in the latest stored collection. The daily EOD seller-level adapter is implemented; "
                    "dashboard levels require successful PostgreSQL persistence. Intraday feed and predictive forecast are not enabled."
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


@app.post("/api/engine/levels")
def engine_levels(payload: dict):
    """Return seller support, resistance and exit-risk zones from validated normalized data."""
    try:
        return build_seller_levels(payload)
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
        "seller_levels": {"endpoint": "POST /api/engine/levels",
                          "support": "PE writing candidates",
                          "resistance": "CE writing candidates",
                          "exit_risk": "unwinding and premium-rise-with-OI-addition",
                          "live_data_connected": False},
    }


@app.get("/api/seller-intelligence/history")
def seller_intelligence_history(before: str, limit: int = 22):
    """Return prior daily seller snapshots for the next 5/22-session calculation."""
    try:
        records = database.get_seller_context_history(before, limit)
    except ValueError:
        raise HTTPException(status_code=400, detail="before must use YYYY-MM-DD.")
    except Exception:
        raise HTTPException(status_code=503, detail="Seller context history is temporarily unavailable.")
    return {"status": "ready" if records else "empty", "records": records}


@app.get("/api/seller-intelligence/latest")
def seller_intelligence_latest():
    """Return the latest daily seller map and its precomputed 5/22-session context."""
    if not database.database_configured():
        return {"status": "unavailable", "reason": "PostgreSQL persistence is not configured."}
    try:
        payload = database.get_cached_json("seller_context:latest")
    except Exception:
        raise HTTPException(status_code=503, detail="Seller context store is temporarily unavailable.")
    return payload if isinstance(payload, dict) else {
        "status": "unavailable", "reason": "No validated daily seller context has been ingested yet."
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
        from datetime import date
        date.fromisoformat(collection_date)
        quality = assess_manifest(manifest)
        declared_session = manifest.get("core_session_date")
        if (not quality.get("aligned") or not quality.get("session_date")
                or declared_session != quality.get("session_date")):
            raise HTTPException(status_code=422, detail="Manifest core reports must pass same-session validation.")
        seller_context = payload.get("seller_context")
        if seller_context is not None:
            if not isinstance(seller_context, dict):
                raise HTTPException(status_code=422, detail="seller_context must be a JSON object.")
            context_date = seller_context.get("session_date")
            current = seller_context.get("current")
            if context_date != quality.get("session_date"):
                raise HTTPException(status_code=422, detail="Seller context session_date must match the validated manifest.")
            if seller_context.get("schema_version") != 1 or seller_context.get("status") != "ready":
                raise HTTPException(status_code=422, detail="Seller context schema/status is not valid.")
            if not isinstance(current, dict) or current.get("status") != "analysis_complete":
                raise HTTPException(status_code=422, detail="Current seller levels are not analysis_complete.")
            if current.get("session_date") != context_date:
                raise HTTPException(status_code=422, detail="Current seller levels date does not match seller context.")
            lookback = seller_context.get("lookback")
            if not isinstance(lookback, dict) or not all(
                isinstance(lookback.get(key), dict)
                for key in ("last_5_sessions", "last_22_sessions")
            ):
                raise HTTPException(status_code=422, detail="Both 5-session and 22-session lookback summaries are required.")
            result = database.upsert_daily_report(collection_date, manifest, seller_context)
        else:
            result = database.upsert_daily_report(collection_date, manifest)
        return {"stored": True, "seller_context_stored": seller_context is not None, **result}
    except HTTPException:
        raise
    except ValueError:
        raise HTTPException(status_code=400, detail="Dates must use YYYY-MM-DD.")
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
