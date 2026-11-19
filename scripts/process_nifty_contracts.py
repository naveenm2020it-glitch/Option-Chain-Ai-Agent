from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from phase3_storage import ensure_phase3_schema, normalize_session, persist_session


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
    if checkpoint.get("schema_version") != 3:
        raise SystemExit("Checkpoint schema_version must be 3")
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

    if args.write_db:
        if not os.getenv("DATABASE_URL"):
            raise SystemExit("--write-db requires DATABASE_URL")
        if errors:
            raise SystemExit(f"Refusing partial DB write: {len(errors)} session(s) failed normalization")
        import psycopg
        url = os.environ["DATABASE_URL"]
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://"):]
        with psycopg.connect(url, connect_timeout=5) as conn:
            ensure_phase3_schema(conn)
            for row in normalized_rows:
                with conn.transaction():
                    persist_session(conn, row)

    report = {
        "schema_version": 1,
        "checkpoint": str(checkpoint_path),
        "sessions_seen": len(session_dates),
        "sessions_normalized": len(summaries),
        "contract_rows": sum(item["contract_count"] for item in summaries),
        "database_written": bool(args.write_db),
        "errors": errors,
        "sessions": summaries,
    }
    print(json.dumps(report, indent=2))
    return 0 if not errors else 2


if __name__ == "__main__":
    raise SystemExit(main())
