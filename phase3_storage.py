from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime, timezone
from typing import Any

SCHEMA_VERSION = 1
OPTION_TYPES = {"CE", "PE"}


def _number(value: Any, field: str, *, minimum: float | None = None,
            maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{field} must be <= {maximum}")
    return result


def normalize_session(session_date: str, row: dict[str, Any],
                      provenance: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validate and normalize one schema-v3 NIFTY historical session."""
    session = date.fromisoformat(session_date)
    if not isinstance(row, dict) or row.get("session_date") != session_date:
        raise ValueError("checkpoint session_date does not match its key")
    if row.get("feature_schema_version") != 3:
        raise ValueError("only feature_schema_version=3 is supported")
    if row.get("contract_universe_version") != 1:
        raise ValueError("only contract_universe_version=1 (all eligible strikes) is supported")
    features = row.get("features")
    contracts = row.get("contracts")
    if not isinstance(features, dict) or not isinstance(contracts, list) or not contracts:
        raise ValueError("features object and non-empty contracts list are required")

    expected_count = features.get("stored_contract_count")
    if expected_count is not None and _number(expected_count, "features.stored_contract_count", minimum=1) != len(contracts):
        raise ValueError("stored_contract_count does not match contracts list")
    normalized = []
    seen = set()
    for index, contract in enumerate(contracts):
        if not isinstance(contract, dict):
            raise ValueError(f"contracts[{index}] must be an object")
        expiry = date.fromisoformat(str(contract.get("expiry", "")))
        if expiry < session:
            raise ValueError(f"contracts[{index}] is expired on the session date")
        option_type = str(contract.get("option_type", "")).strip().upper()
        if option_type not in OPTION_TYPES:
            raise ValueError(f"contracts[{index}] option_type must be CE or PE")
        strike = _number(contract.get("strike"), f"contracts[{index}].strike", minimum=0.01)
        key = (expiry.isoformat(), round(strike, 4), option_type)
        if key in seen:
            raise ValueError(f"duplicate contract key: {key}")
        seen.add(key)

        close = _number(contract.get("close"), f"contracts[{index}].close", minimum=0.01)
        previous_close = _number(contract.get("previous_close", 0.0), f"contracts[{index}].previous_close", minimum=0.0)
        oi = _number(contract.get("oi"), f"contracts[{index}].oi", minimum=0.0)
        change_oi = _number(contract.get("change_oi"), f"contracts[{index}].change_oi")
        volume = _number(contract.get("volume"), f"contracts[{index}].volume", minimum=0.0)
        premium_return = _number(contract.get("premium_return"), f"contracts[{index}].premium_return")
        moneyness = _number(contract.get("moneyness"), f"contracts[{index}].moneyness")
        days_to_expiry = _number(contract.get("days_to_expiry"), f"contracts[{index}].days_to_expiry", minimum=0.0)
        expected_days = float((expiry - session).days)
        if days_to_expiry != expected_days:
            raise ValueError(f"contracts[{index}] days_to_expiry does not match expiry/session dates")
        delta_factor = _number(contract.get("delta_factor", 0.0), f"contracts[{index}].delta_factor", minimum=-1.0, maximum=1.0)
        if option_type == "CE" and delta_factor < 0:
            raise ValueError(f"contracts[{index}] CE delta_factor cannot be negative")
        if option_type == "PE" and delta_factor > 0:
            raise ValueError(f"contracts[{index}] PE delta_factor cannot be positive")

        normalized.append({
            "expiry": expiry.isoformat(), "strike": strike, "option_type": option_type,
            "close": close, "previous_close": previous_close, "oi": oi,
            "change_oi": change_oi, "volume": volume, "premium_return": premium_return,
            "moneyness": moneyness, "days_to_expiry": int(expected_days),
            "delta_factor": delta_factor,
        })

    normalized.sort(key=lambda item: (item["expiry"], item["strike"], item["option_type"]))
    source = provenance or {}
    archive_sha256 = source.get("archive_sha256")
    if archive_sha256 is not None and (
        not isinstance(archive_sha256, str) or len(archive_sha256) != 64
        or any(ch not in "0123456789abcdef" for ch in archive_sha256.lower())
    ):
        raise ValueError("archive_sha256 must be a 64-character hexadecimal digest")
    canonical = json.dumps(
        {"session_date": session_date, "feature_schema_version": 3, "contracts": normalized},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return {
        "session_date": session_date,
        "feature_schema_version": 3,
        "contract_universe_version": 1,
        "contract_count": len(normalized),
        "contracts": normalized,
        "archive_sha256": archive_sha256,
        "catalog_fingerprint": source.get("catalog_fingerprint"),
        "dataset_sha256": hashlib.sha256(canonical).hexdigest(),
    }


def ensure_phase3_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nifty_historical_sessions (
            session_date DATE PRIMARY KEY,
            feature_schema_version SMALLINT NOT NULL CHECK (feature_schema_version = 3),
            contract_universe_version SMALLINT NOT NULL CHECK (contract_universe_version = 1),
            contract_count INTEGER NOT NULL CHECK (contract_count > 0),
            archive_sha256 CHAR(64),
            catalog_fingerprint TEXT,
            dataset_sha256 CHAR(64) NOT NULL,
            processed_at TIMESTAMPTZ NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS nifty_option_contracts (
            session_date DATE NOT NULL REFERENCES nifty_historical_sessions(session_date) ON DELETE CASCADE,
            expiry DATE NOT NULL,
            strike NUMERIC(12, 4) NOT NULL CHECK (strike > 0),
            option_type CHAR(2) NOT NULL CHECK (option_type IN ('CE', 'PE')),
            close NUMERIC(16, 6) NOT NULL CHECK (close > 0),
            previous_close NUMERIC(16, 6) NOT NULL CHECK (previous_close >= 0),
            oi NUMERIC(20, 2) NOT NULL CHECK (oi >= 0),
            change_oi NUMERIC(20, 2) NOT NULL,
            volume NUMERIC(20, 2) NOT NULL CHECK (volume >= 0),
            premium_return DOUBLE PRECISION NOT NULL,
            moneyness DOUBLE PRECISION NOT NULL,
            days_to_expiry INTEGER NOT NULL CHECK (days_to_expiry >= 0),
            delta_factor DOUBLE PRECISION NOT NULL CHECK (delta_factor BETWEEN -1 AND 1),
            PRIMARY KEY (session_date, expiry, strike, option_type),
            CHECK (expiry >= session_date),
            CHECK ((option_type = 'CE' AND delta_factor >= 0)
                OR (option_type = 'PE' AND delta_factor <= 0))
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_nifty_option_contracts_expiry_side
        ON nifty_option_contracts (expiry, option_type, session_date)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_nifty_option_contracts_session_strike
        ON nifty_option_contracts (session_date, strike, option_type)
    """)


def persist_session(conn, normalized: dict[str, Any]) -> None:
    """Atomically replace one session snapshot; safe to rerun idempotently."""
    session_date = normalized["session_date"]
    conn.execute("""
        INSERT INTO nifty_historical_sessions (
            session_date, feature_schema_version, contract_universe_version, contract_count, archive_sha256,
            catalog_fingerprint, dataset_sha256, processed_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (session_date) DO UPDATE SET
            feature_schema_version = EXCLUDED.feature_schema_version,
            contract_universe_version = EXCLUDED.contract_universe_version,
            contract_count = EXCLUDED.contract_count,
            archive_sha256 = EXCLUDED.archive_sha256,
            catalog_fingerprint = EXCLUDED.catalog_fingerprint,
            dataset_sha256 = EXCLUDED.dataset_sha256,
            processed_at = EXCLUDED.processed_at
    """, (
        session_date, normalized["feature_schema_version"], normalized["contract_universe_version"],
        normalized["contract_count"], normalized["archive_sha256"], normalized["catalog_fingerprint"],
        normalized["dataset_sha256"], datetime.now(timezone.utc),
    ))
    conn.execute("DELETE FROM nifty_option_contracts WHERE session_date = %s", (session_date,))
    rows = [(
        session_date, item["expiry"], item["strike"], item["option_type"],
        item["close"], item["previous_close"], item["oi"], item["change_oi"],
        item["volume"], item["premium_return"], item["moneyness"],
        item["days_to_expiry"], item["delta_factor"],
    ) for item in normalized["contracts"]]
    conn.executemany("""
        INSERT INTO nifty_option_contracts (
            session_date, expiry, strike, option_type, close, previous_close,
            oi, change_oi, volume, premium_return, moneyness, days_to_expiry, delta_factor
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, rows)
