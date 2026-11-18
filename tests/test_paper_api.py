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
