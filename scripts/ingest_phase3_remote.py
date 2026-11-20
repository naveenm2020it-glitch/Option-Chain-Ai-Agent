from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def validate_checkpoint(checkpoint_path: Path, coverage_path: Path) -> tuple[dict, dict]:
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    coverage = json.loads(coverage_path.read_text(encoding="utf-8"))
    if checkpoint.get("schema_version") != 2:
        raise ValueError("Checkpoint schema_version must be 2.")
    expected = coverage.get("expected_session_count")
    features = checkpoint.get("features")
    verified = checkpoint.get("verified")
    if not isinstance(features, dict) or not isinstance(verified, dict):
        raise ValueError("Checkpoint features and verified provenance must be objects.")
    if coverage.get("status") != "archive_integrity_complete_calendar_reconciled":
        raise ValueError("Historical archive audit is not complete and calendar-reconciled.")
    if coverage.get("contract_universe_version") != 1:
        raise ValueError("Coverage report is not for all-strike contract_universe_version=1.")
    if coverage.get("model_training_allowed") is not True:
        raise ValueError("Historical coverage gates are not all passed.")
    if coverage.get("feature_error_count") != 0:
        raise ValueError("Feature extraction errors remain in the checkpoint.")
    if coverage.get("verified_session_count") != expected or len(verified) != expected:
        raise ValueError("Verified archive session count does not match the expected calendar.")
    if coverage.get("feature_session_count") != expected or len(features) != expected:
        raise ValueError("Feature session count does not match the expected calendar.")
    if coverage.get("missing_archive_dates") or coverage.get("pending_archive_dates"):
        raise ValueError("Missing or pending archive dates remain.")
    all_strike_coverage = coverage.get("core_market_coverage", {}).get("all_strike_contracts_available", 0)
    if float(all_strike_coverage) < 0.95:
        raise ValueError("All-strike CE/PE contract coverage is below 95%.")
    for session_date, row in features.items():
        if row.get("contract_universe_version") != 1:
            raise ValueError(f"{session_date} is not an all-strike feature row.")
        if session_date not in verified:
            raise ValueError(f"{session_date} has no verified archive provenance.")
    return checkpoint, coverage


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send a validated all-strike historical checkpoint to the protected Render Phase 3 API."
    )
    parser.add_argument("--checkpoint", required=True, help="Path to historical_audit/checkpoint.json")
    parser.add_argument("--coverage", help="Path to sibling coverage.json; defaults beside the checkpoint")
    parser.add_argument("--api-url", required=True, help="Render service base URL")
    parser.add_argument("--batch-size", type=int, default=10, help="Sessions per request (1-10)")
    args = parser.parse_args()

    if not 1 <= args.batch_size <= 10:
        parser.error("--batch-size must be between 1 and 10")
    token = os.getenv("PAPER_INGEST_TOKEN")
    if not token:
        raise SystemExit("PAPER_INGEST_TOKEN must be set in the local environment; do not pass it as a command-line argument.")

    checkpoint_path = Path(args.checkpoint)
    coverage_path = Path(args.coverage) if args.coverage else checkpoint_path.with_name("coverage.json")
    checkpoint, coverage = validate_checkpoint(checkpoint_path, coverage_path)
    features = checkpoint["features"]
    verified = checkpoint["verified"]
    dates = sorted(features)

    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=1.0,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"POST"}),
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"Authorization": f"Bearer {token}", "Content-Type": "application/json"})

    sessions_stored = 0
    contract_rows_stored = 0
    for offset in range(0, len(dates), args.batch_size):
        batch_dates = dates[offset:offset + args.batch_size]
        payload = {
            "sessions": [
                {
                    "session_date": day,
                    "row": features[day],
                    "provenance": verified[day],
                }
                for day in batch_dates
            ]
        }
        response = session.post(
            args.api_url.rstrip("/") + "/api/phase3/ingest-batch",
            json=payload,
            timeout=(10, 120),
        )
        response.raise_for_status()
        result = response.json()
        verification = result.get("verification", {})
        if result.get("stored") is not True or verification.get("passed") is not True:
            raise RuntimeError(f"Server did not verify batch beginning {batch_dates[0]}.")
        if result.get("sessions_stored") != len(batch_dates):
            raise RuntimeError(f"Server stored an unexpected session count for batch {batch_dates[0]}.")
        sessions_stored += int(result["sessions_stored"])
        contract_rows_stored += int(result["contract_rows_stored"])
        print(json.dumps({
            "batch": offset // args.batch_size + 1,
            "sessions_stored": sessions_stored,
            "total_sessions": len(dates),
            "contract_rows_stored": contract_rows_stored,
            "last_session_date": batch_dates[-1],
            "verified": True,
        }), flush=True)

    summary = {
        "status": "complete",
        "sessions_stored": sessions_stored,
        "expected_sessions": coverage["expected_session_count"],
        "contract_rows_stored": contract_rows_stored,
        "all_strike_coverage": coverage["core_market_coverage"]["all_strike_contracts_available"],
        "database_batch_verification": "passed",
    }
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
