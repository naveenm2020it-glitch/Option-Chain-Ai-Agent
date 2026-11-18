from datetime import datetime, timezone
from fastapi.testclient import TestClient
import app

def test_paper_endpoints_require_token(monkeypatch):
    monkeypatch.setenv("PAPER_INGEST_TOKEN","test-secret")
    client=TestClient(app.app)
    r=client.post("/api/paper/quotes",json={})
    assert r.status_code==401

def test_paper_quotes_requires_database(monkeypatch):
    monkeypatch.setenv("PAPER_INGEST_TOKEN","test-secret")
    monkeypatch.delenv("DATABASE_URL",raising=False)
    client=TestClient(app.app)
    r=client.post("/api/paper/quotes",headers={"Authorization":"Bearer test-secret"},json={
      "symbol":"NIFTY","expiry":"2026-11-26","strike":26000,"option_type":"CE",
      "bid":100,"ask":101,"timestamp":datetime.now(timezone.utc).isoformat(),
      "lot_size":65,"source":"test"})
    assert r.status_code==503


def test_paper_performance_accepts_bearer_token(monkeypatch):
    monkeypatch.setenv("PAPER_INGEST_TOKEN", "test-secret")
    monkeypatch.setattr(app.paper_ledger, "paper_summary", lambda limit: {"limit": limit})
    client = TestClient(app.app)
    r = client.get("/api/paper/performance", headers={"Authorization": "Bearer test-secret"})
    assert r.status_code == 200
    assert r.json() == {"limit": 100}


def test_paper_bearer_auth_is_documented_in_openapi():
    schema = app.app.openapi()
    scheme = schema["components"]["securitySchemes"]["PaperIngestToken"]
    assert scheme["type"] == "http"
    assert scheme["scheme"] == "bearer"
    assert schema["paths"]["/api/paper/performance"]["get"]["security"] == [{"PaperIngestToken": []}]
