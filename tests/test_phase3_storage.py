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
