# Render private-network PostgreSQL access

## Goal

The application accesses PostgreSQL from Render's Singapore region over Render's private network. End users can use the web application from any device or network without adding home, office, or mobile IP addresses to the database allowlist.

## Current infrastructure facts (checked 2026-11-19)

- Web service: `option-chain-ai-agent`, Singapore, branch `feature/paper-trading-engine-v1`.
- PostgreSQL: `nifty-predictor-db`, Singapore, PostgreSQL 18.
- PostgreSQL external IP allowlist is empty.
- Web service and database are in the same region.
- Do not change the web service's deployed branch for this networking setup.
- Database is on the Free plan and is scheduled to expire on 2026-12-10.

## One-time Render configuration

1. Open the PostgreSQL resource in the Render Dashboard.
2. Copy its **Internal Database URL**, not the External Database URL.
3. Open the existing `option-chain-ai-agent` web service and its Environment page.
4. Set `DATABASE_URL` to the Internal Database URL. Keep it secret; never commit it or expose it to browser JavaScript.
5. Save the environment change and allow Render to restart the service.
6. Leave the PostgreSQL IP allowlist empty. Do not add `0.0.0.0/0`.

The app's existing `database.py` reads `DATABASE_URL` on the server and normalizes the legacy `postgres://` scheme. Since the service and database are both in Singapore, the internal URL is the intended connection path.

## Read-only connectivity check

From a Render shell for the existing web service, run:

```bash
python scripts/check_database_connection.py
```

The check prints the database name, PostgreSQL version, current user, and whether the Phase 3 tables exist. It never prints the connection URL or credentials and performs no writes.

## Phase 3 data load

After connectivity succeeds, make the audited checkpoint available at:

```text
data/historical_audit/checkpoint.json
```

Then run from the Render service shell:

```bash
python scripts/process_nifty_contracts.py --checkpoint data/historical_audit/checkpoint.json --write-db
```

The processor creates the Phase 3 tables, persists the 494 normalized sessions, and verifies session counts, contract row counts, and per-session SHA-256 fingerprints. A successful exit and `database_verification.passed: true` are required for acceptance.

## Security and operational boundaries

- User devices connect only to the web application; they never connect directly to PostgreSQL.
- PostgreSQL remains off the public network (empty external IP allowlist).
- No database URL or password belongs in the repository, client-side code, logs, or chat.
- This setup does not change the deployed Git branch, app start command, B2 objects, or market-data sources.
- Resolve the Free database's 2026-12-10 expiry before treating it as durable production storage.
