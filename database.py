from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any


def database_configured() -> bool:
    return bool(os.getenv("DATABASE_URL"))


def _connect():
    if not database_configured():
        raise RuntimeError("DATABASE_URL is not configured")
    import psycopg
    url = os.environ["DATABASE_URL"]
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return psycopg.connect(url, connect_timeout=5)


def _ensure_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS app_json_cache (
            cache_key TEXT PRIMARY KEY,
            payload JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_reports (
            collection_date DATE PRIMARY KEY,
            session_date DATE,
            status TEXT NOT NULL,
            manifest JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def get_cached_json(key: str) -> Any | None:
    if not database_configured():
        return None
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT payload FROM app_json_cache WHERE cache_key = %s", (key,)
        ).fetchone()
        return row[0] if row else None


def set_cached_json(key: str, payload: Any) -> None:
    if not database_configured():
        return
    encoded = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """INSERT INTO app_json_cache(cache_key, payload, updated_at)
               VALUES (%s, %s::jsonb, NOW())
               ON CONFLICT(cache_key) DO UPDATE
               SET payload = EXCLUDED.payload, updated_at = NOW()""",
            (key, encoded),
        )


def delete_cached_json(key: str) -> None:
    if not database_configured():
        return
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute("DELETE FROM app_json_cache WHERE cache_key = %s", (key,))


def upsert_daily_report(collection_date: str, manifest: dict[str, Any], seller_context: dict[str, Any] | None = None) -> dict[str, Any]:
    from datetime import date
    date.fromisoformat(collection_date)
    results = manifest.get("results", {}) if isinstance(manifest, dict) else {}
    report_dates = sorted({
        str(item["report_date"]) for item in results.values()
        if isinstance(item, dict) and item.get("report_date")
    })
    report_count = sum(
        1 for item in results.values()
        if isinstance(item, dict) and item.get("file")
        and item.get("status", "downloaded") in {"downloaded", "extracted", "ok"}
    )
    session_date = manifest.get("core_session_date") if isinstance(manifest, dict) else None
    status = manifest.get("core_session_status", "unknown") if isinstance(manifest, dict) else "unknown"
    with _connect() as conn:
        _ensure_schema(conn)
        conn.execute(
            """INSERT INTO daily_reports(collection_date, session_date, status, manifest, updated_at)
               VALUES (%s, %s, %s, %s::jsonb, NOW())
               ON CONFLICT(collection_date) DO UPDATE SET
                 session_date = EXCLUDED.session_date,
                 status = EXCLUDED.status,
                 manifest = EXCLUDED.manifest,
                 updated_at = NOW()""",
            (collection_date, session_date, status,
             json.dumps(manifest, separators=(",", ":"), ensure_ascii=False)),
        )
        if seller_context is not None:
            conn.execute(
                """INSERT INTO app_json_cache(cache_key, payload, updated_at)
                   VALUES ('seller_context:latest', %s::jsonb, NOW())
                   ON CONFLICT(cache_key) DO UPDATE
                   SET payload = EXCLUDED.payload, updated_at = NOW()""",
                (json.dumps(seller_context, separators=(",", ":"), ensure_ascii=False),),
            )
    return {
        "collection_date": collection_date,
        "report_count": report_count,
        "report_dates": report_dates,
        "session_date": session_date,
        "status": status,
        "storage": "postgresql",
    }


def list_daily_reports() -> list[dict[str, Any]]:
    if not database_configured():
        return []
    with _connect() as conn:
        _ensure_schema(conn)
        rows = conn.execute(
            """SELECT collection_date::text, session_date::text, status, manifest
               FROM daily_reports ORDER BY collection_date"""
        ).fetchall()
    entries = []
    for collection_date, session_date, status, manifest in rows:
        results = manifest.get("results", {}) if isinstance(manifest, dict) else {}
        report_dates = sorted({
            str(item["report_date"]) for item in results.values()
            if isinstance(item, dict) and item.get("report_date")
        })
        report_count = sum(
            1 for item in results.values()
            if isinstance(item, dict) and item.get("file")
            and item.get("status", "downloaded") in {"downloaded", "extracted", "ok"}
        )
        entries.append({
            "collection_date": collection_date,
            "session_date": session_date,
            "status": status,
            "report_count": report_count,
            "report_dates": report_dates,
            "storage": "postgresql",
        })
    return entries


def get_daily_report_manifest(collection_date: str) -> dict[str, Any] | None:
    from datetime import date
    date.fromisoformat(collection_date)
    if not database_configured():
        return None
    with _connect() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            "SELECT manifest FROM daily_reports WHERE collection_date = %s",
            (collection_date,),
        ).fetchone()
        return row[0] if row else None
