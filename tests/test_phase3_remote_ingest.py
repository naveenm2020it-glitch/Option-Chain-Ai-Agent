import json

import pytest

from scripts.ingest_phase3_remote import validate_checkpoint


def _write_inputs(tmp_path, *, universe_version=1):
    checkpoint_path = tmp_path / "checkpoint.json"
    coverage_path = tmp_path / "coverage.json"
    checkpoint = {
        "schema_version": 2,
        "features": {
            "2026-11-18": {
                "session_date": "2026-11-18",
                "feature_schema_version": 3,
                "contract_universe_version": universe_version,
                "features": {"stored_ce_contract_count": 1, "stored_pe_contract_count": 1},
                "contracts": [],
            }
        },
        "verified": {"2026-11-18": {"archive_sha256": "a" * 64}},
    }
    coverage = {
        "status": "archive_integrity_complete_calendar_reconciled",
        "expected_session_count": 1,
        "verified_session_count": 1,
        "feature_session_count": 1,
        "feature_error_count": 0,
        "contract_universe_version": 1,
        "model_training_allowed": True,
        "missing_archive_dates": [],
        "pending_archive_dates": [],
        "core_market_coverage": {"all_strike_contracts_available": 1.0},
    }
    checkpoint_path.write_text(json.dumps(checkpoint), encoding="utf-8")
    coverage_path.write_text(json.dumps(coverage), encoding="utf-8")
    return checkpoint_path, coverage_path


def test_remote_ingest_requires_complete_all_strike_checkpoint(tmp_path):
    checkpoint_path, coverage_path = _write_inputs(tmp_path)
    checkpoint, coverage = validate_checkpoint(checkpoint_path, coverage_path)
    assert list(checkpoint["features"]) == ["2026-11-18"]
    assert coverage["model_training_allowed"] is True


def test_remote_ingest_rejects_legacy_contract_universe(tmp_path):
    checkpoint_path, coverage_path = _write_inputs(tmp_path, universe_version=0)
    with pytest.raises(ValueError, match="not an all-strike"):
        validate_checkpoint(checkpoint_path, coverage_path)
