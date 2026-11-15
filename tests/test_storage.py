import json
from unittest.mock import Mock

from storage import b2_configured, read_catalog


def test_b2_requires_all_credentials(monkeypatch):
    for key in ("B2_ENDPOINT_URL", "B2_APPLICATION_KEY_ID", "B2_APPLICATION_KEY", "B2_BUCKET_NAME"):
        monkeypatch.delenv(key, raising=False)
    assert b2_configured() is False


def test_b2_configured_when_all_values_exist(monkeypatch):
    values = {
        "B2_ENDPOINT_URL": "https://s3.us-west-004.backblazeb2.com",
        "B2_APPLICATION_KEY_ID": "test-key-id",
        "B2_APPLICATION_KEY": "test-application-key",
        "B2_BUCKET_NAME": "nifty-history",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert b2_configured() is True


def test_read_catalog_returns_empty_for_missing_key(monkeypatch):
    monkeypatch.setenv("B2_BUCKET_NAME", "nifty-history")

    class MissingKey(Exception):
        response = {"Error": {"Code": "NoSuchKey"}}

    client = Mock()
    client.get_object.side_effect = MissingKey()
    assert read_catalog(client) == {
        "schema_version": 1, "updated_at_utc": None, "collections": []
    }


def test_read_catalog_parses_existing_catalog(monkeypatch):
    monkeypatch.setenv("B2_BUCKET_NAME", "nifty-history")
    payload = {"schema_version": 1, "collections": [{"collection_date": "2026-11-12"}]}
    body = Mock()
    body.read.return_value = json.dumps(payload).encode()
    client = Mock()
    client.get_object.return_value = {"Body": body}
    assert read_catalog(client) == payload



def test_read_catalog_uses_process_cache_for_default_client(monkeypatch):
    import storage

    bucket = "cache-test-catalog"
    monkeypatch.setenv("B2_BUCKET_NAME", bucket)
    body = Mock()
    body.read.return_value = json.dumps({"schema_version": 1, "collections": []}).encode()
    client = Mock()
    client.get_object.return_value = {"Body": body}
    monkeypatch.setattr(storage, "b2_client", lambda: client)

    first = storage.read_catalog()
    second = storage.read_catalog()

    assert first == second
    assert client.get_object.call_count == 1


def test_read_catalog_does_not_cache_explicit_client(monkeypatch):
    import storage

    monkeypatch.setenv("B2_BUCKET_NAME", "explicit-client-test")
    body = Mock()
    body.read.return_value = json.dumps({"schema_version": 1, "collections": []}).encode()
    client = Mock()
    client.get_object.return_value = {"Body": body}

    storage.read_catalog(client)
    storage.read_catalog(client)

    assert client.get_object.call_count == 2


def test_upload_collection_reuses_catalog_and_uses_bounded_puts(tmp_path, monkeypatch):
    import storage

    monkeypatch.setenv("B2_BUCKET_NAME", "nifty-history")
    report_dir = tmp_path / "2026-11-13"
    report_dir.mkdir()
    (report_dir / "fo_bhavcopy.csv").write_text("symbol,close\nNIFTY,25000\n")
    (report_dir / "manifest.json").write_text(json.dumps({
        "results": {
            "fo_udiff_bhavcopy": {
                "status": "downloaded",
                "report_date": "2026-11-13",
                "file": "fo_bhavcopy.csv",
                "sha256": "test-hash",
            }
        }
    }))

    client = Mock()
    catalog = {"schema_version": 1, "collections": []}
    monkeypatch.setattr(storage, "_cache_delete", Mock())
    monkeypatch.setattr(storage.database, "delete_cached_json", Mock())

    entry = storage.upload_collection(
        report_dir,
        "2026-11-13",
        client=client,
        catalog=catalog,
    )

    assert entry["collection_date"] == "2026-11-13"
    assert entry["report_count"] == 1
    assert entry["archive_bytes"] > 0
    assert len(entry["archive_sha256"]) == 64
    assert catalog["collections"] == [entry]
    assert client.get_object.call_count == 0
    assert client.put_object.call_count == 3
    assert client.put_object.call_args_list[0].kwargs["Key"] == "nse/daily/2026-11-13/reports.zip"
    assert client.put_object.call_args_list[1].kwargs["Key"] == "nse/daily/2026-11-13/manifest.json"
    assert client.put_object.call_args_list[2].kwargs["Key"] == "nse/catalog.json"


def test_upload_collection_survives_optional_database_cache_invalidation_failure(tmp_path, monkeypatch):
    import storage

    monkeypatch.setenv("B2_BUCKET_NAME", "nifty-history")
    report_dir = tmp_path / "2026-11-13"
    report_dir.mkdir()
    (report_dir / "manifest.json").write_text(json.dumps({"results": {}}))

    client = Mock()
    monkeypatch.setattr(storage, "read_catalog", lambda supplied: {
        "schema_version": 1, "collections": []
    })
    monkeypatch.setattr(storage, "_cache_delete", Mock())
    monkeypatch.setattr(
        storage.database,
        "delete_cached_json",
        Mock(side_effect=RuntimeError("database temporarily unavailable")),
    )

    entry = storage.upload_collection(report_dir, "2026-11-13", client=client)

    assert entry["collection_date"] == "2026-11-13"
    assert client.put_object.call_count == 3
