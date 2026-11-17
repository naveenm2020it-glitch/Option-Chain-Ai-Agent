from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


def _b2_region() -> str:
    explicit = os.environ.get("B2_REGION")
    if explicit:
        return explicit
    endpoint = os.environ.get("B2_ENDPOINT_URL", "")
    match = re.search(r"s3[.]([a-z0-9-]+)[.]backblazeb2[.]com", endpoint, re.IGNORECASE)
    return match.group(1) if match else "us-east-1"


def archive_directory_to_b2(source_dir: Path, object_key: str) -> dict:
    """Upload one immutable tar.gz archive to Backblaze B2's S3-compatible API.

    Required environment:
      B2_APPLICATION_KEY_ID, B2_APPLICATION_KEY, B2_BUCKET_NAME,
      B2_ENDPOINT_URL (for example https://s3.us-west-004.backblazeb2.com)
    """
    required = (
        "B2_APPLICATION_KEY_ID",
        "B2_APPLICATION_KEY",
        "B2_BUCKET_NAME",
        "B2_ENDPOINT_URL",
    )
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("B2 archive is enabled but required environment variables are missing: " + ", ".join(missing))

    import gzip
    import io
    import tarfile

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for path in sorted(source_dir.rglob("*")):
                if path.is_file():
                    info = archive.gettarinfo(str(path), arcname=path.relative_to(source_dir).as_posix())
                    info.mtime = 0
                    with path.open("rb") as source:
                        archive.addfile(info, source)
    payload = buffer.getvalue()
    digest = hashlib.sha256(payload).hexdigest()

    client = boto3.client(
        "s3",
        endpoint_url=os.environ["B2_ENDPOINT_URL"].rstrip("/"),
        aws_access_key_id=os.environ["B2_APPLICATION_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APPLICATION_KEY"],
        region_name=_b2_region(),
    )
    bucket = os.environ["B2_BUCKET_NAME"]
    try:
        existing = client.head_object(Bucket=bucket, Key=object_key)
        existing_digest = (existing.get("Metadata") or {}).get("sha256")
        if existing_digest == digest:
            return {"status": "already_present", "bucket": bucket, "key": object_key,
                    "bytes": len(payload), "sha256": digest, "b2_operations": 1}
        raise RuntimeError(
            f"B2 object already exists with different content at {object_key}; refusing to overwrite immutable archive"
        )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code not in ("404", "NoSuchKey", "NotFound") and status != 404:
            raise

    client.put_object(
        Bucket=bucket,
        Key=object_key,
        Body=payload,
        ContentType="application/gzip",
        Metadata={"sha256": digest},
    )
    return {"status": "uploaded", "bucket": bucket, "key": object_key,
            "bytes": len(payload), "sha256": digest, "b2_operations": 2}



def download_archive_from_b2(destination_dir: Path, object_key: str) -> dict:
    """Download and safely extract the rolling System 1 archive; missing object is a cache miss."""
    required = ("B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET_NAME", "B2_ENDPOINT_URL")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("B2 cache is enabled but required environment variables are missing: " + ", ".join(missing))

    import io
    import tarfile

    client = boto3.client(
        "s3",
        endpoint_url=os.environ["B2_ENDPOINT_URL"].rstrip("/"),
        aws_access_key_id=os.environ["B2_APPLICATION_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APPLICATION_KEY"],
        region_name=os.environ.get("B2_REGION", "us-east-1"),
    )
    bucket = os.environ["B2_BUCKET_NAME"]
    try:
        response = client.get_object(Bucket=bucket, Key=object_key)
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if code in ("404", "NoSuchKey", "NotFound") or status == 404:
            return {"status": "cache_miss", "bucket": bucket, "key": object_key, "b2_operations": 1}
        raise

    payload = response["Body"].read()
    digest = hashlib.sha256(payload).hexdigest()
    expected = (response.get("Metadata") or {}).get("sha256")
    if expected and expected != digest:
        raise RuntimeError("B2 rolling archive SHA-256 metadata mismatch")
    destination_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        root = destination_dir.resolve()
        for member in archive.getmembers():
            target = (destination_dir / member.name).resolve()
            if root != target and root not in target.parents:
                raise RuntimeError("B2 archive contains an unsafe path")
            if not member.isfile() and not member.isdir():
                raise RuntimeError("B2 archive contains an unsupported link or special file")
        archive.extractall(destination_dir, filter="data")
    return {"status": "cache_loaded", "bucket": bucket, "key": object_key,
            "bytes": len(payload), "sha256": digest, "b2_operations": 1}


def upload_rolling_archive_to_b2(source_dir: Path, object_key: str) -> dict:
    """Replace the single rolling snapshot after a successful integrity audit."""
    required = ("B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET_NAME", "B2_ENDPOINT_URL")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError("B2 rolling archive is enabled but required environment variables are missing: " + ", ".join(missing))

    import gzip
    import io
    import tarfile

    buffer = io.BytesIO()
    with gzip.GzipFile(fileobj=buffer, mode="wb", mtime=0) as compressed:
        with tarfile.open(fileobj=compressed, mode="w") as archive:
            for path in sorted(source_dir.rglob("*")):
                if path.is_file():
                    info = archive.gettarinfo(str(path), arcname=path.relative_to(source_dir).as_posix())
                    info.mtime = 0
                    with path.open("rb") as source:
                        archive.addfile(info, source)
    payload = buffer.getvalue()
    digest = hashlib.sha256(payload).hexdigest()
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["B2_ENDPOINT_URL"].rstrip("/"),
        aws_access_key_id=os.environ["B2_APPLICATION_KEY_ID"],
        aws_secret_access_key=os.environ["B2_APPLICATION_KEY"],
        region_name=os.environ.get("B2_REGION", "us-east-1"),
    )
    bucket = os.environ["B2_BUCKET_NAME"]
    client.put_object(Bucket=bucket, Key=object_key, Body=payload,
                      ContentType="application/gzip", Metadata={"sha256": digest})
    return {"status": "rolling_snapshot_updated", "bucket": bucket, "key": object_key,
            "bytes": len(payload), "sha256": digest, "b2_operations": 1}
