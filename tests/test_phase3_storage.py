from __future__ import annotations

import pytest

from phase3_storage import normalize_session


def sample():
    return {
        "session_date": "2026-11-18",
        "feature_schema_version": 3,
        "features": {"spot_close": 25000.0},
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


def test_normalizes_and_sorts_contracts_deterministically():
    row = sample()
    result = normalize_session(row["session_date"], row, {"archive_sha256": "a" * 64})
    assert result["contract_count"] == 2
    assert [x["option_type"] for x in result["contracts"]] == ["CE", "PE"]
    assert len(result["dataset_sha256"]) == 64
    assert result["archive_sha256"] == "a" * 64


def test_rejects_duplicate_contract_keys():
    row = sample()
    row["contracts"].append(dict(row["contracts"][0]))
    with pytest.raises(ValueError, match="duplicate contract key"):
        normalize_session(row["session_date"], row)


def test_rejects_expired_contract():
    row = sample()
    row["contracts"][0]["expiry"] = "2026-11-17"
    with pytest.raises(ValueError, match="expired"):
        normalize_session(row["session_date"], row)


def test_rejects_expiry_horizon_mismatch():
    row = sample()
    row["contracts"][0]["days_to_expiry"] = 7
    with pytest.raises(ValueError, match="days_to_expiry"):
        normalize_session(row["session_date"], row)


def test_rejects_invalid_option_delta_sign():
    row = sample()
    row["contracts"][1]["delta_factor"] = 0.4
    with pytest.raises(ValueError, match="PE delta_factor"):
        normalize_session(row["session_date"], row)


def test_rejects_non_finite_values():
    row = sample()
    row["contracts"][0]["close"] = float("nan")
    with pytest.raises(ValueError, match="finite"):
        normalize_session(row["session_date"], row)


def test_requires_schema_v3_and_matching_session_key():
    row = sample()
    row["feature_schema_version"] = 2
    with pytest.raises(ValueError, match="feature_schema_version"):
        normalize_session(row["session_date"], row)
    row = sample()
    with pytest.raises(ValueError, match="does not match"):
        normalize_session("2026-11-17", row)


def test_database_verification_matches_session_counts_and_fingerprints():
    from datetime import date
    from scripts.process_nifty_contracts import verify_persisted_sessions

    source = {
        "session_date": "2026-11-18",
        "feature_schema_version": 3,
        "contract_count": 2,
        "dataset_sha256": "b" * 64,
    }

    class FakeConnection:
        def execute(self, sql, params):
            if "FROM nifty_historical_sessions" in sql:
                return self
            if "FROM nifty_option_contracts" in sql:
                return self
            raise AssertionError("unexpected SQL")

        def fetchall(self):
            # The verifier issues the session query before the contract-count query.
            if not hasattr(self, "calls"):
                self.calls = 1
                return [(date(2026, 11, 18), 3, 2, "b" * 64)]
            return [(date(2026, 11, 18), 2)]

    result = verify_persisted_sessions(FakeConnection(), [source])
    assert result["passed"] is True
    assert result["expected_sessions"] == result["actual_sessions"] == 1
    assert result["expected_contract_rows"] == result["actual_contract_rows"] == 2
    assert result["fingerprints_checked"] == 1


def test_database_verification_detects_fingerprint_mismatch():
    from datetime import date
    from scripts.process_nifty_contracts import verify_persisted_sessions

    source = {
        "session_date": "2026-11-18",
        "feature_schema_version": 3,
        "contract_count": 2,
        "dataset_sha256": "b" * 64,
    }

    class FakeConnection:
        def __init__(self):
            self.calls = 0

        def execute(self, sql, params):
            return self

        def fetchall(self):
            self.calls += 1
            if self.calls == 1:
                return [(date(2026, 11, 18), 3, 2, "c" * 64)]
            return [(date(2026, 11, 18), 2)]

    result = verify_persisted_sessions(FakeConnection(), [source])
    assert result["passed"] is False
    assert {"session_date": "2026-11-18", "error": "dataset_sha256_mismatch"} in result["mismatches"]
