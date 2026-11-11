import pytest
from fastapi.testclient import TestClient

import app as app_module
from nifty_ai import database


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(database, "DB_PATH", tmp_path / "api-tests.sqlite3")
    monkeypatch.setattr(app_module, "APP_PASSWORD", "")
    database.init_db()
    with TestClient(app_module.app) as test_client:
        yield test_client


def test_health_and_root_are_available(client):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert health.json()["app"] == "NIFTY Next-Day AI Intelligence"
    root = client.get("/")
    assert root.status_code == 200
    assert "text/html" in root.headers["content-type"]
    assert "NIFTY" in root.text


def test_read_only_api_contracts_return_expected_shapes(client):
    expected = {
        "/api/state": ("reports", "report_counts", "report_catalog", "agent_registry", "data_quality"),
        "/api/reports": ("items",),
        "/api/analyses": ("items",),
        "/api/prices": ("items",),
        "/api/config": ("incoming_folder", "data_folder", "render_mode", "max_upload_mb"),
    }
    for path, keys in expected.items():
        response = client.get(path)
        assert response.status_code == 200, (path, response.text)
        payload = response.json()
        assert all(key in payload for key in keys), (path, payload)


def test_analysis_without_imported_reports_returns_clear_client_error(client):
    response = client.post("/api/analyze")
    assert response.status_code == 400
    assert "No reports found" in response.json()["detail"]


def test_upload_rejects_unsupported_file_type(client):
    response = client.post(
        "/api/import",
        files=[("files", ("payload.exe", b"not a report", "application/octet-stream"))],
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 0
    assert payload["errors"]
    assert "Unsupported file" in payload["errors"][0]["error"]


def test_password_protected_api_rejects_missing_and_wrong_password(client, monkeypatch):
    monkeypatch.setattr(app_module, "APP_PASSWORD", "test-only-password")
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/state", headers={"X-App-Password": "wrong"}).status_code == 401
    accepted = client.get("/api/state", headers={"X-App-Password": "test-only-password"})
    assert accepted.status_code == 200
