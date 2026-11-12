from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any


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
    client = client or b2_client()
    bucket = os.environ["B2_BUCKET_NAME"]
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


def read_collection_manifest(collection_date: str) -> dict[str, Any]:
    """Read one archived NSE collection manifest from B2."""
    from datetime import date
    date.fromisoformat(collection_date)  # strict YYYY-MM-DD validation
    client = b2_client()
    response = client.get_object(
        Bucket=os.environ["B2_BUCKET_NAME"],
        Key=f"nse/daily/{collection_date}/manifest.json",
    )
    return json.loads(response["Body"].read().decode("utf-8"))


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


def upload_collection(directory, collection_date: str) -> dict[str, Any]:
    from pathlib import Path
    import hashlib
    import zipfile

    root = Path(directory)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError("manifest.json is required before archiving")
    archive_path = root.parent / f"nse-eod-{collection_date}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.iterdir()):
            if path.is_file():
                archive.write(path, arcname=path.name)

    digest = hashlib.sha256(archive_path.read_bytes()).hexdigest()
    prefix = f"nse/daily/{collection_date}"
    bucket = os.environ["B2_BUCKET_NAME"]
    client = b2_client()
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
