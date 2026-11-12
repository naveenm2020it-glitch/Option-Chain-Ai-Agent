import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from app import app

client = TestClient(app)

def test_health_endpoint():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"

def test_status_does_not_publish_fake_prediction():
    response = client.get("/api/status")
    assert response.status_code == 200
    assert response.json()["market"] == "NIFTY 50"
    assert response.json()["options_scope"] == "NIFTY only"
    assert response.json()["prediction"] is None

def test_dashboard_loads():
    response = client.get("/")
    assert response.status_code == 200
    assert "NIFTY 50 Intelligence" in response.text
