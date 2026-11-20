import hashlib
import io
import tarfile
from pathlib import Path

import pytest
from botocore.exceptions import ClientError

from system1 import b2_archive


class FakeS3:
    def __init__(self, existing=None):
        self.existing = existing
        self.puts = []

    def head_object(self, Bucket, Key):
        if self.existing is None:
            raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "HeadObject")
        return {"Metadata": {"sha256": self.existing}}

    def put_object(self, **kwargs):
        self.puts.append(kwargs)

    def get_object(self, Bucket, Key):
        raise ClientError({"Error": {"Code": "404"}, "ResponseMetadata": {"HTTPStatusCode": 404}}, "GetObject")


def set_b2_env(monkeypatch):
    monkeypatch.setenv("B2_APPLICATION_KEY_ID", "test-id")
    monkeypatch.setenv("B2_APPLICATION_KEY", "test-key")
    monkeypatch.setenv("B2_BUCKET_NAME", "test-bucket")
    monkeypatch.setenv("B2_ENDPOINT_URL", "https://s3.example.test")


def test_archive_requires_all_b2_settings(tmp_path, monkeypatch):
    for name in ("B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET_NAME", "B2_ENDPOINT_URL"):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(RuntimeError, match="required environment variables"):
        b2_archive.archive_directory_to_b2(tmp_path, "archive.tar.gz")


def test_archive_uploads_once_and_records_hash(tmp_path, monkeypatch):
    set_b2_env(monkeypatch)
    (tmp_path / "session").mkdir()
    (tmp_path / "session" / "manifest.json").write_text('{"ok": true}')
    fake = FakeS3()
    monkeypatch.setattr(b2_archive.boto3, "client", lambda *a, **k: fake)
    result = b2_archive.archive_directory_to_b2(tmp_path, "system1/test.tar.gz")
    assert result["status"] == "uploaded"
    assert result["b2_operations"] == 2
    assert len(fake.puts) == 1
    assert fake.puts[0]["Bucket"] == "test-bucket"
    assert fake.puts[0]["Metadata"]["sha256"] == result["sha256"]


def test_archive_is_idempotent_when_same_hash_exists(tmp_path, monkeypatch):
    set_b2_env(monkeypatch)
    (tmp_path / "manifest.json").write_text("same content")
    first_fake = FakeS3()
    monkeypatch.setattr(b2_archive.boto3, "client", lambda *a, **k: first_fake)
    first = b2_archive.archive_directory_to_b2(tmp_path, "system1/test.tar.gz")
    second_fake = FakeS3(existing=first["sha256"])
    monkeypatch.setattr(b2_archive.boto3, "client", lambda *a, **k: second_fake)
    result = b2_archive.archive_directory_to_b2(tmp_path, "system1/test.tar.gz")
    assert result["status"] == "already_present"
    assert result["sha256"] == first["sha256"]
    assert second_fake.puts == []

def test_archive_refuses_to_overwrite_different_existing_content(tmp_path, monkeypatch):
    set_b2_env(monkeypatch)
    (tmp_path / "manifest.json").write_text("changed content")
    fake = FakeS3(existing="different")
    monkeypatch.setattr(b2_archive.boto3, "client", lambda *a, **k: fake)
    with pytest.raises(RuntimeError, match="refusing to overwrite"):
        b2_archive.archive_directory_to_b2(tmp_path, "system1/test.tar.gz")



def test_download_cache_miss_is_reported_without_failing(tmp_path, monkeypatch):
    set_b2_env(monkeypatch)
    fake = FakeS3()
    monkeypatch.setattr(b2_archive.boto3, "client", lambda *a, **k: fake)
    result = b2_archive.download_archive_from_b2(tmp_path, "rolling.tar.gz")
    assert result["status"] == "cache_miss"
    assert result["b2_operations"] == 1


def test_download_archive_extracts_verified_files(tmp_path, monkeypatch):
    set_b2_env(monkeypatch)
    payload_buffer = io.BytesIO()
    with tarfile.open(fileobj=payload_buffer, mode="w:gz") as archive:
        data = b'{"session":"2026-11-17"}'
        info = tarfile.TarInfo("2026-11-17/manifest.json")
        info.size = len(data)
        archive.addfile(info, io.BytesIO(data))
    payload = payload_buffer.getvalue()

    class DownloadS3(FakeS3):
        def get_object(self, Bucket, Key):
            return {"Body": io.BytesIO(payload),
                    "Metadata": {"sha256": hashlib.sha256(payload).hexdigest()}}

    monkeypatch.setattr(b2_archive.boto3, "client", lambda *a, **k: DownloadS3())
    result = b2_archive.download_archive_from_b2(tmp_path, "rolling.tar.gz")
    assert result["status"] == "cache_loaded"
    assert (tmp_path / "2026-11-17" / "manifest.json").read_text() == '{"session":"2026-11-17"}'


def test_rolling_snapshot_upload_uses_single_put(tmp_path, monkeypatch):
    set_b2_env(monkeypatch)
    (tmp_path / "manifest.json").write_text("snapshot")
    fake = FakeS3()
    monkeypatch.setattr(b2_archive.boto3, "client", lambda *a, **k: fake)
    result = b2_archive.upload_rolling_archive_to_b2(tmp_path, "system1/rolling/snapshot.tar.gz")
    assert result["status"] == "rolling_snapshot_updated"
    assert result["b2_operations"] == 1
    assert len(fake.puts) == 1


def test_rolling_archive_operations_derive_region_from_b2_endpoint(tmp_path, monkeypatch):
    set_b2_env(monkeypatch)
    monkeypatch.setenv("B2_ENDPOINT_URL", "https://s3.us-west-004.backblazeb2.com")
    monkeypatch.delenv("B2_REGION", raising=False)
    clients = []

    def make_client(*args, **kwargs):
        clients.append(kwargs)
        return FakeS3()

    monkeypatch.setattr(b2_archive.boto3, "client", make_client)
    download = b2_archive.download_archive_from_b2(tmp_path / "download", "rolling.tar.gz")
    upload = b2_archive.upload_rolling_archive_to_b2(tmp_path, "rolling.tar.gz")

    assert download["status"] == "cache_miss"
    assert upload["status"] == "rolling_snapshot_updated"
    assert len(clients) == 2
    assert all(client["region_name"] == "us-west-004" for client in clients)


def test_explicit_b2_region_overrides_endpoint_detection(monkeypatch):
    monkeypatch.setenv("B2_REGION", "eu-central-003")
    monkeypatch.setenv("B2_ENDPOINT_URL", "https://s3.us-west-004.backblazeb2.com")
    assert b2_archive._b2_region() == "eu-central-003"
