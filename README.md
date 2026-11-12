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
- GET /api/history — durable historical collection catalog (requires R2 configuration)

## Durable historical storage (Cloudflare R2)
The scheduled GitHub Actions collector saves a dated ZIP archive and its manifest to:
- `nse/daily/YYYY-MM-DD/reports.zip`
- `nse/daily/YYYY-MM-DD/manifest.json`
- `nse/catalog.json`

The date in the key is the collection date, not a claim that every report belongs to that trading session. Original per-report dates and SHA-256 values remain in the manifest. Re-running a collection date replaces that date's archive and catalog entry (idempotent daily key).

Create an R2 Standard bucket, then create an R2 API token with **Object Read & Write** permission restricted to that bucket. Add these GitHub repository Actions secrets:
- `R2_ENDPOINT_URL` — `https://<ACCOUNT_ID>.r2.cloudflarestorage.com`
- `R2_ACCESS_KEY_ID`
- `R2_SECRET_ACCESS_KEY`
- `R2_BUCKET_NAME`

For the deployed web app, configure the same four values as environment variables. Never put credentials in source code or commit them. If the secrets are absent, collection still runs and only the temporary 7-day GitHub artifact is retained; the workflow does not claim a permanent archive was saved.

Cloudflare R2 pricing and setup: https://developers.cloudflare.com/r2/pricing/ and https://developers.cloudflare.com/r2/get-started/s3/.

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
