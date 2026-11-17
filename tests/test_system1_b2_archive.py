import hashlib
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
