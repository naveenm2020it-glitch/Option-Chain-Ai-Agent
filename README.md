# NIFTY 50 Next-Day AI Market Intelligence

A mobile-first, evidence-led research dashboard for **NIFTY 50** and **NIFTY index options only**.

## Current status
The app has a FastAPI backend, mobile dashboard, NSE report collector, data-quality gate and CI tests. It intentionally shows **no prediction** until real source data has been collected and validated.

## Run
Install dependencies: `pip install -r requirements.txt`
Start server: `uvicorn app:app --host 0.0.0.0 --port 8000`
Open http://localhost:8000.

## API
- GET /api/health — service health
- GET /api/status — selected market, report count and data readiness
- GET /api/history — durable historical collection catalog (requires Backblaze B2 configuration)

## Durable historical storage (Backblaze B2)
The scheduled GitHub Actions collector saves a dated ZIP archive and its manifest to:
- `nse/daily/YYYY-MM-DD/reports.zip`
- `nse/daily/YYYY-MM-DD/manifest.json`
- `nse/catalog.json`

The date in the key is the collection date, not a claim that every report belongs to that trading session. Original per-report dates and SHA-256 values remain in the manifest. Re-running a collection date replaces that date's archive and catalog entry (idempotent daily key).

Create a **private** Backblaze B2 bucket, then create an Application Key restricted to that bucket with read/write access. Add these GitHub repository Actions secrets:
- `B2_ENDPOINT_URL` — exact S3 endpoint shown for your bucket, e.g. `https://s3.us-west-004.backblazeb2.com` (use your actual region)
- `B2_APPLICATION_KEY_ID` — the Application Key ID
- `B2_APPLICATION_KEY` — the Application Key secret (shown only once)
- `B2_BUCKET_NAME` — your bucket name, suggested: `nifty-history`

For the deployed web app, configure the same four values as environment variables. Never put credentials in source code or commit them. If the secrets are absent, collection still runs and only the temporary 7-day GitHub artifact is retained; the workflow does not claim a permanent archive was saved.

Backblaze setup: https://www.backblaze.com/docs/en/cloud-storage-get-started-with-a-backblaze-integration
Pricing: https://www.backblaze.com/cloud-storage/pricing

## Deployment
Render build command: `pip install -r requirements.txt`
Render start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`

## Principles
- Free data sources where possible; source provenance and file hashes retained.
- No fabricated or stale data presented as current.
- Open interest alone does not identify who bought or sold.
- Forecasts are frozen before the next session and scored without hindsight.
- Directional accuracy is distinct from options trading profitability.
- Research support only, not financial advice.

See docs/ARCHITECTURE.md for the architecture.


## Durable history metadata and daily report ingestion

The application supports an optional PostgreSQL database through `DATABASE_URL`.

- PostgreSQL stores the historical catalog cache, dated manifests, and each daily collector manifest.
- The daily collector posts its manifest to `POST /api/internal/daily-report` using `Authorization: Bearer <DAILY_INGEST_TOKEN>`.
- Configure these Render environment variables: `DATABASE_URL` and `DAILY_INGEST_TOKEN`.
- Configure these GitHub Actions repository secrets: `APP_INGEST_URL` (the deployed app base URL, for example `https://nifty50-next-day-intelligence.onrender.com`) and the same `DAILY_INGEST_TOKEN`.
- Generate a long random ingestion token and keep it only in Render/GitHub secrets. Never commit it.
- If PostgreSQL is not configured, daily database ingestion is skipped and the existing B2 archive workflow remains unchanged.
- If B2 is temporarily unavailable, already cached catalog/manifests and daily manifests in PostgreSQL can still be served. Raw ZIP archive downloads still require B2.

**Production note:** Do not use Render's Free Postgres for permanent market history. Render documents that Free Postgres is limited to 1 GB, expires after 30 days, and has no backups. Use a persistent database plan/provider before relying on it for production history. Until a durable database URL and ingestion secrets are configured, this feature remains inactive.
