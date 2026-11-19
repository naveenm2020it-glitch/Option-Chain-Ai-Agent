from __future__ import annotations

import argparse
import json
import os
from datetime import date
from pathlib import Path

from phase3_storage import ensure_phase3_schema, normalize_session, persist_session


def verify_persisted_sessions(conn, normalized_rows: list[dict]) -> dict:
    """Compare persisted session metadata and contract counts with source rows."""
    if not normalized_rows:
        raise ValueError("No normalized sessions supplied for database verification")

    dates = [date.fromisoformat(row["session_date"]) for row in normalized_rows]
    expected = {row["session_date"]: row for row in normalized_rows}
    session_rows = conn.execute(
        """
        SELECT session_date, feature_schema_version, contract_count, dataset_sha256
        FROM nifty_historical_sessions
        WHERE session_date = ANY(%s)
        ORDER BY session_date
        """,
        (dates,),
    ).fetchall()
    contract_rows = conn.execute(
        """
        SELECT session_date, COUNT(*)::bigint
        FROM nifty_option_contracts
        WHERE session_date = ANY(%s)
        GROUP BY session_date
        ORDER BY session_date
        """,
        (dates,),
    ).fetchall()

    actual_sessions = {
        row[0].isoformat(): {
            "feature_schema_version": int(row[1]),
            "contract_count": int(row[2]),
            "dataset_sha256": str(row[3]).strip(),
        }
        for row in session_rows
    }
    actual_contract_counts = {row[0].isoformat(): int(row[1]) for row in contract_rows}

    mismatches = []
    for session_date, source in expected.items():
        actual = actual_sessions.get(session_date)
        actual_contract_count = actual_contract_counts.get(session_date, 0)
        if actual is None:
            mismatches.append({"session_date": session_date, "error": "session_metadata_missing"})
            continue
        if actual["feature_schema_version"] != source["feature_schema_version"]:
            mismatches.append({"session_date": session_date, "error": "feature_schema_version_mismatch"})
        if actual["contract_count"] != source["contract_count"]:
            mismatches.append({
                "session_date": session_date,
                "error": "session_contract_count_metadata_mismatch",
                "expected": source["contract_count"],
                "actual": actual["contract_count"],
            })
        if actual_contract_count != source["contract_count"]:
            mismatches.append({
                "session_date": session_date,
                "error": "persisted_contract_row_count_mismatch",
                "expected": source["contract_count"],
                "actual": actual_contract_count,
            })
        if actual["dataset_sha256"] != source["dataset_sha256"]:
            mismatches.append({"session_date": session_date, "error": "dataset_sha256_mismatch"})

    expected_contract_rows = sum(row["contract_count"] for row in normalized_rows)
    actual_contract_rows = sum(actual_contract_counts.values())
    return {
        "passed": not mismatches and len(actual_sessions) == len(expected),
        "expected_sessions": len(expected),
        "actual_sessions": len(actual_sessions),
        "expected_contract_rows": expected_contract_rows,
        "actual_contract_rows": actual_contract_rows,
        "fingerprints_checked": len(actual_sessions),
        "mismatches": mismatches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Normalize schema-v3 NIFTY option snapshots; DB writes require --write-db."
    )
    parser.add_argument("--checkpoint", required=True, help="Historical audit checkpoint JSON")
    parser.add_argument("--write-db", action="store_true", help="Persist normalized snapshots to PostgreSQL")
    parser.add_argument("--limit", type=int, default=0, help="Optional maximum sessions; 0 means all")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit cannot be negative")

    checkpoint_path = Path(args.checkpoint)
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    if checkpoint.get("schema_version") != 2:
        raise SystemExit("Checkpoint schema_version must be 2 (feature rows remain schema-v3)")
    features = checkpoint.get("features")
    verified = checkpoint.get("verified", {})
    if not isinstance(features, dict) or not features:
        raise SystemExit("Checkpoint contains no feature sessions")
    if not isinstance(verified, dict):
        raise SystemExit("Checkpoint verified provenance must be an object")

    session_dates = sorted(features)
    if args.limit:
        session_dates = session_dates[:args.limit]
    summaries = []
    errors = []
    normalized_rows = []
    for session_date in session_dates:
        try:
            row = normalize_session(session_date, features[session_date], verified.get(session_date, {}))
            summaries.append({
                "session_date": session_date,
                "contract_count": row["contract_count"],
                "dataset_sha256": row["dataset_sha256"],
            })
            normalized_rows.append(row)
        except Exception as exc:
            errors.append({"session_date": session_date, "error": f"{type(exc).__name__}: {exc}"})

    database_verification = None
    if args.write_db:
        if not os.getenv("DATABASE_URL"):
            raise SystemExit("--write-db requires DATABASE_URL")
        if errors:
            raise SystemExit(f"Refusing partial DB write: {len(errors)} session(s) failed normalization")
        import psycopg
        url = os.environ["DATABASE_URL"]
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        with psycopg.connect(url, connect_timeout=10) as conn:
            ensure_phase3_schema(conn)
            for row in normalized_rows:
                # One transaction per session: a failed session rolls back without
                # invalidating earlier committed sessions; reruns replace each snapshot.
                with conn.transaction():
                    persist_session(conn, row)
            database_verification = verify_persisted_sessions(conn, normalized_rows)

    report = {
        "schema_version": 2,
        "checkpoint": str(checkpoint_path),
        "sessions_seen": len(session_dates),
        "sessions_normalized": len(summaries),
        "contract_rows": sum(item["contract_count"] for item in summaries),
        "database_written": bool(args.write_db),
        "database_verification": database_verification,
        "errors": errors,
        "sessions": summaries,
    }
    print(json.dumps(report, indent=2))
    if errors:
        return 2
    if database_verification is not None and not database_verification["passed"]:
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
