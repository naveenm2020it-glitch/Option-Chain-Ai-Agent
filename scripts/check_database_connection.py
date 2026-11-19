from __future__ import annotations

import os


def main() -> int:
    url = os.getenv("DATABASE_URL")
    if not url:
        print("DATABASE_URL is not configured")
        return 2

    import psycopg

    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    try:
        with psycopg.connect(url, connect_timeout=10, autocommit=True) as conn:
            row = conn.execute(
                "SELECT current_database(), current_user, version()"
            ).fetchone()
            tables = conn.execute(
                """
                SELECT table_name
                FROM information_schema.tables
                WHERE table_schema = 'public'
                  AND table_name IN (
                    'nifty_historical_sessions',
                    'nifty_option_contracts'
                  )
                ORDER BY table_name
                """
            ).fetchall()
    except Exception as exc:
        # Do not print the exception text: a malformed connection URL could
        # contain credentials or other sensitive connection information.
        print(f"Database connection failed: {type(exc).__name__}")
        return 1

    print(f"database: {row[0]}")
    print(f"user: {row[1]}")
    print(f"server: {row[2].splitlines()[0]}")
    existing = {item[0] for item in tables}
    print(f"phase3_sessions_table: {'present' if 'nifty_historical_sessions' in existing else 'missing'}")
    print(f"phase3_contracts_table: {'present' if 'nifty_option_contracts' in existing else 'missing'}")
    print("read_only: true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
