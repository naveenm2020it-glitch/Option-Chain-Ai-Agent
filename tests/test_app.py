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


def test_history_api_explains_unconfigured_storage(monkeypatch):
    for key in ("R2_ENDPOINT_URL", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_NAME"):
        monkeypatch.delenv(key, raising=False)
    response = client.get("/api/history")
    assert response.status_code == 200
    payload = response.json()
    assert payload["configured"] is False
    assert payload["collections"] == []


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



def test_engine_models_reports_clean_reset_without_legacy_modules():
    response = client.get("/api/engine/models")
    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "reset_pending_rebuild"
    assert payload["parallel_subprocesses"] == []
    assert payload["models"] == []
    assert payload["live_data_connected"] is False


def test_engine_analysis_is_disabled_during_model_rebuild():
    response = client.post("/api/engine/analyze", json={})
    assert response.status_code == 409
    assert "reset" in response.json()["detail"].lower()


def test_dashboard_marks_option_chain_as_unavailable_until_validated_feed():
    response = client.get("/")
    assert response.status_code == 200
    assert 'id="option-chain"' in response.text
    assert 'id="engine-grid"' in response.text
    assert "No validated option-chain snapshot loaded." in response.text
    assert "No sample prices or open-interest values are shown." in response.text



def test_dashboard_caches_b2_backed_metadata_in_session():
    response = client.get("/static/app.js")
    assert response.status_code == 200
    script = response.text
    assert "sessionStorage" in script
    assert "STATUS_CACHE_TTL_MS=5*60*1000" in script
    assert "HISTORY_CACHE_TTL_MS=10*60*1000" in script
    assert 'fetchJsonWithSessionCache("/api/status",STATUS_CACHE_TTL_MS)' in script
    assert 'fetchJsonWithSessionCache("/api/history",HISTORY_CACHE_TTL_MS)' in script
    assert "cached" in script and "repeat archive reads" in script


def test_dashboard_distinguishes_b2_credentials_from_quota_status():
    response = client.get("/")
    script = client.get("/static/app.js")
    assert response.status_code == 200
    assert script.status_code == 200
    assert "B2 quota status" in response.text
    assert "Not queried by app" in response.text
    assert "Credentials set" in script.text
    assert "Not queried by app" in script.text


def test_dashboard_has_explicit_intelligence_sections():
    response = client.get("/")
    assert response.status_code == 200
    for label in ("AI Analysis", "SELLER INTELLIGENCE", "RISK ANALYSIS", "Historical Data", "Daily Reports", "System health"):
        assert label in response.text


def test_daily_report_ingestion_requires_bearer_token(monkeypatch):
    monkeypatch.setenv("DAILY_INGEST_TOKEN", "expected-secret")
    response = client.post(
        "/api/internal/daily-report",
        json={"collection_date": "2026-11-13", "manifest": {"results": {}}},
    )
    assert response.status_code == 401


def test_daily_report_ingestion_persists_manifest(monkeypatch):
    monkeypatch.setenv("DAILY_INGEST_TOKEN", "expected-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setattr("database.upsert_daily_report", lambda day, manifest: {
        "collection_date": day, "report_count": 4, "storage": "postgresql"
    })
    response = client.post(
        "/api/internal/daily-report",
        headers={"Authorization": "Bearer expected-secret"},
        json={"collection_date": "2026-11-13", "manifest": {"results": {}}},
    )
    assert response.status_code == 200
    assert response.json()["stored"] is True
    assert response.json()["report_count"] == 4


def test_daily_manifest_is_served_from_database_without_b2(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://test")
    monkeypatch.setattr("database.get_daily_report_manifest", lambda day: {
        "requested_end_date": day, "results": {}
    })
    monkeypatch.setattr("storage.b2_configured", lambda: False)
    response = client.get("/api/history/2026-11-13")
    assert response.status_code == 200
    assert response.json()["storage"] == "postgresql"
    assert response.json()["manifest"]["requested_end_date"] == "2026-11-13"


def test_status_reads_latest_collection_manifest_from_b2(monkeypatch, tmp_path):
    import app as app_module

    manifest = {
        "core_session_status": "aligned",
        "core_session_date": "2026-11-13",
        "results": {
            name: {
                "status": "downloaded",
                "report_date": "2026-11-13",
                "file": name + ".csv",
            }
            for name in CORE_REPORTS
        },
    }
    monkeypatch.setattr(app_module, "DATA_DIR", tmp_path)
    monkeypatch.setattr(app_module, "b2_configured", lambda: True)
    monkeypatch.setattr(app_module, "history_catalog", lambda: {
        "collections": [{"collection_date": "2026-11-13"}]
    })
    monkeypatch.setattr(app_module, "read_collection_manifest", lambda day: manifest)

    response = client.get("/api/status")

    assert response.status_code == 200
    payload = response.json()
    assert payload["data_source"] == "backblaze_b2"
    assert payload["latest_collection_date"] == "2026-11-13"
    assert payload["data_mode"] == "validated_eod"
    assert payload["data_quality"]["session_date"] == "2026-11-13"
    assert payload["report_files"] == len(CORE_REPORTS)



def test_phase3_schema_initialization_requires_paper_bearer_token(monkeypatch):
    monkeypatch.setenv("PAPER_INGEST_TOKEN", "expected-secret")
    response = client.post("/api/phase3/initialize-schema")
    assert response.status_code == 401


def test_phase3_schema_initialization_creates_only_schema(monkeypatch):
    import psycopg

    monkeypatch.setenv("PAPER_INGEST_TOKEN", "expected-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://not-a-real-secret")
    statements = []

    class FakeConnection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def execute(self, query, params=None):
            statements.append(query)

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: FakeConnection())
    response = client.post(
        "/api/phase3/initialize-schema",
        headers={"Authorization": "Bearer expected-secret"},
    )
    assert response.status_code == 200
    assert response.json() == {
        "schema_initialized": True,
        "historical_rows_loaded": False,
        "tables": ["nifty_historical_sessions", "nifty_option_contracts"],
    }
    assert sum("CREATE TABLE IF NOT EXISTS" in sql for sql in statements) == 2
    assert sum("CREATE INDEX IF NOT EXISTS" in sql for sql in statements) == 2
    # Inspect the leading SQL command; DDL constraints may contain the text ON DELETE.
    assert all(sql.lstrip().split(None, 1)[0].upper() == "CREATE" for sql in statements)


def test_phase3_batch_ingestion_requires_paper_bearer_token(monkeypatch):
    monkeypatch.setenv("PAPER_INGEST_TOKEN", "expected-secret")
    response = client.post("/api/phase3/ingest-batch", json={"sessions": []})
    assert response.status_code == 401


def test_phase3_batch_ingestion_persists_and_verifies_normalized_sessions(monkeypatch):
    from contextlib import nullcontext
    import psycopg

    monkeypatch.setenv("PAPER_INGEST_TOKEN", "expected-secret")
    monkeypatch.setenv("DATABASE_URL", "postgresql://not-a-real-secret")
    persisted = []

    class FakeConnection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def transaction(self):
            return nullcontext()

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: FakeConnection())
    monkeypatch.setattr("phase3_storage.ensure_phase3_schema", lambda conn: None)
    monkeypatch.setattr("phase3_storage.persist_session", lambda conn, row: persisted.append(row))
    monkeypatch.setattr("scripts.process_nifty_contracts.verify_persisted_sessions", lambda conn, rows: {
        "passed": True, "expected_sessions": len(rows), "actual_sessions": len(rows),
        "expected_contract_rows": sum(row["contract_count"] for row in rows),
        "actual_contract_rows": sum(row["contract_count"] for row in rows),
        "fingerprints_checked": len(rows), "mismatches": [],
    })

    row = {
        "session_date": "2026-11-18",
        "feature_schema_version": 3,
        "contract_universe_version": 1,
        "features": {"spot_close": 25000.0, "stored_contract_count": 2},
        "contracts": [
            {
                "expiry": "2026-11-26", "strike": 25000, "option_type": "CE",
                "close": 120.5, "previous_close": 110.0, "oi": 1000,
                "change_oi": 100, "volume": 5000, "premium_return": 0.09545,
                "moneyness": 0.0, "days_to_expiry": 8, "delta_factor": 0.52,
            },
            {
                "expiry": "2026-11-26", "strike": 25000, "option_type": "PE",
                "close": 115.0, "previous_close": 125.0, "oi": 900,
                "change_oi": -50, "volume": 4200, "premium_return": -0.08,
                "moneyness": 0.0, "days_to_expiry": 8, "delta_factor": -0.48,
            },
        ],
    }
    response = client.post(
        "/api/phase3/ingest-batch",
        headers={"Authorization": "Bearer expected-secret"},
        json={"sessions": [{
            "session_date": "2026-11-18",
            "row": row,
            "provenance": {"archive_sha256": "a" * 64},
        }]},
    )
    assert response.status_code == 200
    assert response.json()["sessions_stored"] == 1
    assert response.json()["contract_rows_stored"] == 2
    assert response.json()["verification"]["passed"] is True
    assert len(persisted) == 1
    assert persisted[0]["contract_universe_version"] == 1


def test_phase3_batch_ingestion_rejects_legacy_contract_universe(monkeypatch):
    monkeypatch.setenv("PAPER_INGEST_TOKEN", "expected-secret")
    row = {
        "session_date": "2026-11-18",
        "feature_schema_version": 3,
        "features": {"stored_contract_count": 1},
        "contracts": [{
            "expiry": "2026-11-26", "strike": 25000, "option_type": "CE",
            "close": 120.5, "previous_close": 110.0, "oi": 1000,
            "change_oi": 100, "volume": 5000, "premium_return": 0.09545,
            "moneyness": 0.0, "days_to_expiry": 8, "delta_factor": 0.52,
        }],
    }
    response = client.post(
        "/api/phase3/ingest-batch",
        headers={"Authorization": "Bearer expected-secret"},
        json={"sessions": [{"session_date": "2026-11-18", "row": row, "provenance": {}}]},
    )
    assert response.status_code == 400
    assert "contract_universe_version" in response.json()["detail"]


def test_phase3_database_health_reports_unconfigured_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    response = client.get("/api/phase3/health")
    assert response.status_code == 200
    assert response.json() == {
        "database_configured": False,
        "database_connected": False,
        "phase3_sessions_table": False,
        "phase3_contracts_table": False,
    }


def test_phase3_database_health_checks_tables_read_only(monkeypatch):
    import psycopg

    monkeypatch.setenv("DATABASE_URL", "postgresql://not-a-real-secret")
    class FakeResult:
        def fetchall(self):
            return [("nifty_historical_sessions",)]

    class FakeConnection:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def execute(self, query):
            assert "information_schema.tables" in query
            return FakeResult()

    monkeypatch.setattr(psycopg, "connect", lambda *args, **kwargs: FakeConnection())
    response = client.get("/api/phase3/health")
    assert response.status_code == 200
    assert response.json() == {
        "database_configured": True,
        "database_connected": True,
        "phase3_sessions_table": True,
        "phase3_contracts_table": False,
    }


def test_phase3_database_health_hides_connection_error(monkeypatch):
    import psycopg

    monkeypatch.setenv("DATABASE_URL", "postgresql://not-a-real-secret")
    def fail_connect(*args, **kwargs):
        raise RuntimeError("sensitive connection details")
    monkeypatch.setattr(psycopg, "connect", fail_connect)
    response = client.get("/api/phase3/health")
    assert response.status_code == 503
    assert "sensitive connection details" not in response.text
