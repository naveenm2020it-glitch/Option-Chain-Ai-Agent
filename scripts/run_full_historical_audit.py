from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.nse_historical_audit import run_audit


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the resumable all-strike NIFTY historical audit to its final acceptance gate."
    )
    parser.add_argument("--start", default="2024-11-12")
    parser.add_argument("--end", default="2026-11-12")
    parser.add_argument("--checkpoint", default="data/historical_audit/checkpoint.json")
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--max-batches", type=int, default=5)
    args = parser.parse_args()

    if not 1 <= args.batch_size <= 25:
        parser.error("--batch-size must be between 1 and 25")
    if args.max_batches < 1:
        parser.error("--max-batches must be positive")

    checkpoint = Path(args.checkpoint)
    final_report = None
    for batch_number in range(1, args.max_batches + 1):
        report = run_audit(
            date.fromisoformat(args.start),
            date.fromisoformat(args.end),
            checkpoint,
            args.batch_size,
        )
        final_report = report
        print(json.dumps({
            "batch": batch_number,
            "status": report.get("status"),
            "processed_this_run": report.get("processed_this_run"),
            "verified_session_count": report.get("verified_session_count"),
            "feature_session_count": report.get("feature_session_count"),
            "all_strike_coverage": report.get("core_market_coverage", {}).get("all_strike_contracts_available"),
            "b2_cap_blocked": report.get("b2_cap_blocked"),
        }), flush=True)

        if report.get("status") == "paused_b2_class_b_cap":
            print("Audit paused safely at the B2 request cap. Preserve this checkpoint and resume in a later run.", flush=True)
            return 3
        if report.get("status") == "archive_integrity_complete_calendar_reconciled":
            coverage = report.get("core_market_coverage", {})
            gates = (
                report.get("expected_session_count") == 494
                and report.get("verified_session_count") == 494
                and report.get("feature_session_count") == 494
                and report.get("failed_session_count") == 0
                and report.get("feature_error_count") == 0
                and report.get("contract_universe_version") == 1
                and float(coverage.get("all_strike_contracts_available", 0.0)) >= 0.95
                and not report.get("missing_archive_dates")
                and not report.get("pending_archive_dates")
            )
            if not gates:
                print("Archive audit completed, but one or more all-strike acceptance gates failed.", flush=True)
                return 4
            print("PHASE 1 ALL-STRIKE AUDIT ACCEPTANCE GATES PASSED.", flush=True)
            return 0
        if int(report.get("processed_this_run", 0)) == 0:
            print("Audit made no progress and did not reach the completion state; inspect coverage.json.", flush=True)
            return 5

    print(json.dumps(final_report or {}, indent=2), flush=True)
    print("Maximum audit batches reached before completion; checkpoint is resumable.", flush=True)
    return 6


if __name__ == "__main__":
    raise SystemExit(main())
