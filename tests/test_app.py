import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient
from data_quality import assess_manifest, CORE_REPORTS
from app import app

client = TestClient(app)


def test_health_endpoint():
    response = client.get("/api/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_status_does_not_publish_fake_prediction():
    response = client.get("/api/status")
    assert response.status_code == 200
    payload = response.json()
    assert payload["market"] == "NIFTY 50"
    assert payload["options_scope"] == "NIFTY only"
    assert payload["prediction"] is None
    assert payload["data_mode"] == "blocked_data_quality"


def test_dashboard_loads():
    response = client.get("/")
    assert response.status_code == 200
    assert "NIFTY 50 Intelligence" in response.text


def make_manifest(dates):
    return {
        "results": {
            name: {
                "status": "downloaded",
                "report_date": dates[name],
                "file": name + ".csv",
            }
            for name in CORE_REPORTS
        }
    }


def test_mixed_core_report_dates_block_analysis():
    dates = {name: "2026-11-11" for name in CORE_REPORTS}
    dates["fo_market_activity"] = "2026-11-12"
    quality = assess_manifest(make_manifest(dates))
    assert quality["status"] == "blocked"
    assert quality["aligned"] is False
    assert quality["session_date"] is None
    assert any("different sessions" in issue for issue in quality["issues"])


def test_same_core_report_date_passes_alignment_gate():
    dates = {name: "2026-11-12" for name in CORE_REPORTS}
    quality = assess_manifest(make_manifest(dates))
    assert quality["status"] == "ready"
    assert quality["aligned"] is True
    assert quality["session_date"] == "2026-11-12"
    assert quality["issues"] == []


def test_missing_core_report_blocks_analysis():
    manifest = make_manifest({name: "2026-11-12" for name in CORE_REPORTS})
    del manifest["results"]["fo_participant_oi"]
    quality = assess_manifest(manifest)
    assert quality["status"] == "blocked"
    assert quality["aligned"] is False
    assert any("fo_participant_oi" in issue for issue in quality["issues"])


def test_manifest_without_results_is_blocked():
    quality = assess_manifest(None)
    assert quality["status"] == "blocked"
    assert quality["aligned"] is False
