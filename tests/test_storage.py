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
