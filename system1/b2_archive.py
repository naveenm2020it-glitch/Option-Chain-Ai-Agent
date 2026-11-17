from __future__ import annotations

import hashlib
import os
from pathlib import Path

import boto3
from botocore.exceptions import ClientError


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

    import io
    import tarfile

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for path in sorted(source_dir.rglob("*")):
            if path.is_file():
                archive.add(path, arcname=path.relative_to(source_dir).as_posix())
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
