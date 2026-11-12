import json
import os
from unittest.mock import Mock

import pytest

from storage import r2_configured, read_catalog


def test_r2_requires_all_credentials(monkeypatch):
    for key in ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"):
        monkeypatch.delenv(key, raising=False)
    assert r2_configured() is False


def test_r2_configured_when_all_values_exist(monkeypatch):
    values = {
        "R2_ENDPOINT_URL": "https://example.r2.cloudflarestorage.com",
        "R2_ACCESS_KEY_ID": "test-access",
        "R2_SECRET_ACCESS_KEY": "test-secret",
        "R2_BUCKET_NAME": "nifty-history",
    }
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    assert r2_configured() is True


def test_read_catalog_returns_empty_for_missing_key(monkeypatch):
    monkeypatch.setenv("R2_BUCKET_NAME", "nifty-history")

    class MissingKey(Exception):
        response = {"Error": {"Code": "NoSuchKey"}}

    client = Mock()
    client.get_object.side_effect = MissingKey()
    assert read_catalog(client) == {
        "schema_version": 1, "updated_at_utc": None, "collections": []
    }


def test_read_catalog_parses_existing_catalog(monkeypatch):
    monkeypatch.setenv("R2_BUCKET_NAME", "nifty-history")
    payload = {"schema_version": 1, "collections": [{"collection_date": "2026-11-12"}]}
    body = Mock()
    body.read.return_value = json.dumps(payload).encode()
    client = Mock()
    client.get_object.return_value = {"Body": body}
    assert read_catalog(client) == payload
