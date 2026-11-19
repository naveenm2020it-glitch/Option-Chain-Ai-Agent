# Phase 3 — Storage and data processing

## Scope

- Preserve the existing Backblaze B2 archive layout and PostgreSQL service.
- Read the existing historical audit checkpoint; do not fetch live or new market data.
- Normalize the schema-v3 NIFTY CE/PE contract snapshots into a relational PostgreSQL representation.
- Preserve archive provenance and deterministic dataset fingerprints.
- Make each session write atomic and safe to rerun.
- Keep database writes opt-in; the default command is a read-only validation/dry run.
- Do not train models, place orders, deploy services, or change infrastructure.

## Existing source contract

The input is `data/historical_audit/checkpoint.json`:
- Checkpoint envelope `schema_version = 2`.
- Per-session feature row `feature_schema_version = 3`.
- Each feature row contains `session_date`, `features`, and `contracts`.
- Each contract is keyed by expiry, strike, and option type (CE/PE).

Only records already present in the checkpoint are processed. The script does not contact B2 or NSE.

## PostgreSQL tables

`nifty_historical_sessions` stores one row per trading session, including feature schema, contract count, source archive SHA-256, catalog fingerprint, deterministic normalized dataset SHA-256, and processing timestamp.

`nifty_option_contracts` stores one row per session/expiry/strike/option-type with close, previous close, OI, change in OI, volume, premium return, moneyness, days to expiry, and delta factor.

Primary keys and checks enforce unique contract identity, positive strike/close, nonnegative OI/volume, valid CE/PE, expiry not before session, and signed delta conventions. Foreign-key cascade keeps a session snapshot internally consistent.

## Run

Read-only normalization and validation (no DB connection or writes):

```bash
python scripts/process_nifty_contracts.py --checkpoint data/historical_audit/checkpoint.json
```

Limit a validation run:

```bash
python scripts/process_nifty_contracts.py --checkpoint data/historical_audit/checkpoint.json --limit 5
```

Explicit PostgreSQL persistence (requires DATABASE_URL and operator approval):

```bash
python scripts/process_nifty_contracts.py --checkpoint data/historical_audit/checkpoint.json --write-db
```

A session is normalized completely before it is written. If any session fails validation, the script refuses the entire requested DB write rather than persisting a partial set. Each session replacement is transactional and idempotent.

## Safety and completion boundary

- No B2 writes, B2 reads, NSE calls, live market feeds, model training, trading, or deployment.
- No production database write is performed by CI.
- CI tests schema and normalization invariants with synthetic fixtures.
- Full Phase 3 acceptance still requires running the read-only processor against the approved historical checkpoint, reviewing the report, and separately approving any PostgreSQL write.
- The script does not persist the 182-dimensional Phase 2 model feature vector; that remains the Phase 2 research dataset artifact. This phase stores the normalized contract snapshots and session provenance needed for later feature engineering.
