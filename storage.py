from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any


def r2_configured() -> bool:
    required = ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME")
    return all(os.getenv(key) for key in required)


def r2_client():
    if not r2_configured():
        raise RuntimeError("R2 is not configured; set R2_ENDPOINT_URL, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY and R2_BUCKET_NAME")
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def read_catalog(client=None) -> dict[str, Any]:
    client = client or r2_client()
    bucket = os.environ["R2_BUCKET_NAME"]
    try:
        response = client.get_object(Bucket=bucket, Key="nse/catalog.json")
        return json.loads(response["Body"].read().decode("utf-8"))
    except Exception as exc:
        error_code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if error_code in {"NoSuchKey", "404", "NotFound"}:
            return {"schema_version": 1, "updated_at_utc": None, "collections": []}
        raise


def history_catalog() -> dict[str, Any]:
    return read_catalog()


def upload_collection(directory, collection_date: str) -> dict[str, Any]:
    from pathlib import Path
    import hashlib
    import zipfile

    root = Path(directory)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("manifest.json is required before archiving")
    # The collection date identifies when this batch was collected. Individual
    # reports retain their own report_date; it is not assumed to be the session date.
    archive_path = root.parent / f"nse-eod-{collection_date}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.iterdir()):
            if path.is_file():
                archive.write(path, arcname=path.name)

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    prefix = f"nse/daily/{collection_date}"
    bucket = os.environ["R2_BUCKET_NAME"]
    client = r2_client()
    client.upload_file(str(archive_path), bucket, f"{prefix}/reports.zip",
                       ExtraArgs={"ContentType": "application/zip"})
    client.upload_file(str(manifest_path), bucket, f"{prefix}/manifest.json",
                       ExtraArgs={"ContentType": "application/json"})

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    catalog = read_catalog(client)
    entry = {
        "collection_date": collection_date,
        "archive_key": f"{prefix}/reports.zip",
        "manifest_key": f"{prefix}/manifest.json",
        "archive_sha256": digest,
        "archive_bytes": archive_path.stat().st_size,
        "report_count": sum(1 for item in manifest.get("results", {}).values()
                            if item.get("file") and item.get("status", "downloaded") in {"downloaded", "extracted", "ok"}),
        "report_dates": sorted({item["report_date"] for item in manifest.get("results", {}).values()
                                 if item.get("report_date")}),
    }
    entries = {item["collection_date"]: item for item in catalog.get("collections", [])}
    entries[collection_date] = entry
    catalog = {
        "schema_version": 1,
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "collections": [entries[key] for key in sorted(entries)],
    }
    client.put_object(Bucket=bucket, Key="nse/catalog.json",
                      Body=json.dumps(catalog, indent=2).encode("utf-8"),
                      ContentType="application/json")
    return entry
