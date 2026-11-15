from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import database
from threading import RLock


# Per-process read-through cache: prevent every dashboard visitor from hitting B2.
# Render process restarts/extra instances have independent caches; durable DB caching
# is a later architecture step, not implied by this in-memory layer.
_CACHE_TTL_SECONDS = {"catalog": 300, "manifest": 3600}
_CACHE_MAX_ENTRIES = 512
_CACHE: dict[tuple[str, str], tuple[float, Any]] = {}
_CACHE_LOCK = RLock()


def _cache_get(kind: str, key: str):
    import time
    cache_key = (kind, key)
    now = time.monotonic()
    with _CACHE_LOCK:
        item = _CACHE.get(cache_key)
        if item is None:
            return None
        expires_at, value = item
        if expires_at <= now:
            _CACHE.pop(cache_key, None)
            return None
        return value


def _cache_set(kind: str, key: str, value: Any) -> None:
    import time
    cache_key = (kind, key)
    with _CACHE_LOCK:
        if len(_CACHE) >= _CACHE_MAX_ENTRIES and cache_key not in _CACHE:
            now = time.monotonic()
            expired = [k for k, (expiry, _) in _CACHE.items() if expiry <= now]
            for old_key in expired:
                _CACHE.pop(old_key, None)
            if len(_CACHE) >= _CACHE_MAX_ENTRIES:
                _CACHE.pop(next(iter(_CACHE)))
        _CACHE[cache_key] = (time.monotonic() + _CACHE_TTL_SECONDS[kind], value)


def _cache_delete(kind: str, key: str) -> None:
    with _CACHE_LOCK:
        _CACHE.pop((kind, key), None)


def b2_configured() -> bool:
    required = ("B2_ENDPOINT_URL", "B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET_NAME")
    return all(os.getenv(key) for key in required)


def b2_client():
    if not b2_configured():
        raise RuntimeError(
            "Backblaze B2 is not configured; set B2_ENDPOINT_URL, "
            "B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY and B2_BUCKET_NAME"
        )
    import boto3
    from botocore.config import Config

    endpoint = os.environ["B2_ENDPOINT_URL"].rstrip("/")
    match = re.fullmatch(r"https://s3\.([a-z0-9-]+)\.backblazeb2\.com", endpoint)
    if not match:
        raise RuntimeError("B2_ENDPOINT_URL must match https://s3.<region>.backblazeb2.com")
    region = match.group(1)
    return boto3.client(
        "s3",
        endpoint_url=endpoint,
        aws_access_key_id=os.environ["B2_APPLICATION_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APPLICATION_KEY"],
        region_name=region,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )


def read_catalog(client=None) -> dict[str, Any]:
    bucket = os.environ["B2_BUCKET_NAME"]
    if client is not None:
        # Explicit clients are used by maintenance code and tests; leave them uncached.
        try:
            response = client.get_object(Bucket=bucket, Key="nse/catalog.json")
            return json.loads(response["Body"].read().decode("utf-8"))
        except Exception as exc:
            error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if error_code in {"NoSuchKey", "404", "NotFound"}:
                return {"schema_version": 1, "updated_at_utc": None, "collections": []}
            raise

    # Hold the re-entrant lock across the cache miss and B2 read to prevent
    # simultaneous first-page visitors from stampeding the same B2 object.
    with _CACHE_LOCK:
        cached = _cache_get("catalog", bucket)
        if cached is not None:
            return cached
        client = b2_client()
        try:
            response = client.get_object(Bucket=bucket, Key="nse/catalog.json")
            catalog = json.loads(response["Body"].read().decode("utf-8"))
        except Exception as exc:
            error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
            if error_code in {"NoSuchKey", "404", "NotFound"}:
                return {"schema_version": 1, "updated_at_utc": None, "collections": []}
            raise
        _cache_set("catalog", bucket, catalog)
        return catalog

def history_catalog() -> dict[str, Any]:
    # Shared persistent cache keeps client traffic independent of B2 after warm-up.
    catalog = database.get_cached_json("b2:catalog")
    if catalog is None:
        catalog = read_catalog()
        database.set_cached_json("b2:catalog", catalog)
    merged = {item["collection_date"]: item for item in catalog.get("collections", [])
              if isinstance(item, dict) and item.get("collection_date")}
    # Daily collector metadata is durable in PostgreSQL, even if B2 is temporarily unavailable.
    for item in database.list_daily_reports():
        old = merged.get(item["collection_date"], {})
        merged[item["collection_date"]] = {**item, **old}
    return {**catalog, "collections": [merged[key] for key in sorted(merged)]}


def read_collection_manifest(collection_date: str) -> dict[str, Any]:
    """Read one archived NSE collection manifest from B2."""
    from datetime import date
    date.fromisoformat(collection_date)  # strict YYYY-MM-DD validation
    bucket = os.environ["B2_BUCKET_NAME"]
    persistent_key = f"b2:manifest:{collection_date}"
    persistent = database.get_cached_json(persistent_key)
    if persistent is not None:
        return persistent
    cache_key = f"{bucket}:{collection_date}"
    # Prevent concurrent cache misses for the same dated manifest.
    with _CACHE_LOCK:
        cached = _cache_get("manifest", cache_key)
        if cached is not None:
            return cached
        client = b2_client()
        response = client.get_object(
            Bucket=bucket,
            Key=f"nse/daily/{collection_date}/manifest.json",
        )
        manifest = json.loads(response["Body"].read().decode("utf-8"))
        _cache_set("manifest", cache_key, manifest)
        database.set_cached_json(persistent_key, manifest)
        return manifest

def archive_download_url(collection_date: str, expires_in: int = 900) -> str:
    """Create a short-lived URL for downloading an archived report ZIP."""
    from datetime import date
    date.fromisoformat(collection_date)
    return b2_client().generate_presigned_url(
        "get_object",
        Params={
            "Bucket": os.environ["B2_BUCKET_NAME"],
            "Key": f"nse/daily/{collection_date}/reports.zip",
        },
        ExpiresIn=expires_in,
    )



def upload_collection(directory, collection_date: str, client=None, catalog: dict[str, Any] | None = None) -> dict[str, Any]:
    """Upload one immutable daily bundle and update the shared catalog once.

    Callers processing multiple sessions should pass the same client and mutable
    catalog to avoid a catalog GET for every session.
    """
    from datetime import date
    from pathlib import Path
    import hashlib
    import zipfile

    date.fromisoformat(collection_date)
    root = Path(directory)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("manifest.json is required before archiving")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    results = manifest.get("results", {}) if isinstance(manifest, dict) else {}
    if not isinstance(results, dict):
        raise ValueError("manifest.results must be an object")

    archive_path = root.parent / f"nse-eod-{collection_date}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.iterdir()):
            if path.is_file():
                archive.write(path, arcname=path.name)

    archive_bytes = archive_path.read_bytes()
    digest = hashlib.sha256(archive_bytes).hexdigest()
    prefix = f"nse/daily/{collection_date}"
    bucket = os.environ["B2_BUCKET_NAME"]
    client = client or b2_client()
    # Read the catalog before writing any object. A caller doing a batch can
    # supply and reuse its in-memory catalog, keeping this to one GET per run.
    current_catalog = catalog if catalog is not None else read_catalog(client)

    entry = {
        "collection_date": collection_date,
        "archive_key": f"{prefix}/reports.zip",
        "manifest_key": f"{prefix}/manifest.json",
        "archive_sha256": digest,
        "archive_bytes": len(archive_bytes),
        "report_count": sum(
            1 for item in results.values()
            if isinstance(item, dict) and item.get("file")
            and item.get("status", "downloaded") in {"downloaded", "extracted", "ok"}
        ),
        "report_dates": sorted({
            str(item["report_date"]) for item in results.values()
            if isinstance(item, dict) and item.get("report_date")
        }),
    }

    entries = {
        item["collection_date"]: item
        for item in current_catalog.get("collections", [])
        if isinstance(item, dict) and item.get("collection_date")
    }
    entries[collection_date] = entry
    updated_catalog = {
        "schema_version": 1,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "collections": [entries[key] for key in sorted(entries)],
    }

    # Use single PUTs rather than transfer-manager multipart uploads for the
    # small daily bundles. This keeps request counts predictable and bounded.
    client.put_object(
        Bucket=bucket,
        Key=f"{prefix}/reports.zip",
        Body=archive_bytes,
        ContentType="application/zip",
        Metadata={"sha256": digest},
    )
    client.put_object(
        Bucket=bucket,
        Key=f"{prefix}/manifest.json",
        Body=manifest_path.read_bytes(),
        ContentType="application/json",
    )
    client.put_object(
        Bucket=bucket,
        Key="nse/catalog.json",
        Body=json.dumps(updated_catalog, indent=2).encode("utf-8"),
        ContentType="application/json",
    )

    # Keep the caller's batch catalog in sync without another B2 read.
    if catalog is not None:
        catalog.clear()
        catalog.update(updated_catalog)

    _cache_delete("catalog", bucket)
    _cache_delete("manifest", f"{bucket}:{collection_date}")
    # The B2 archive is already durable. A stale optional DB cache must never
    # turn a successful B2 upload into a failed collection job.
    try:
        database.delete_cached_json("b2:catalog")
        database.delete_cached_json(f"b2:manifest:{collection_date}")
    except Exception:
        pass
    return entry
