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
