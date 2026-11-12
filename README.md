# NIFTY 50 Next-Day AI Market Intelligence

A mobile-first, evidence-led research dashboard for **NIFTY 50** and **NIFTY index options only**.

## Current status
Phase 1 foundation committed: FastAPI backend, mobile dashboard, health/status API, architecture document and CI smoke tests. The app intentionally shows **no prediction** until real source data has been collected and validated.

## Run
Install dependencies: pip install -r requirements.txt
Start server: uvicorn app:app --host 0.0.0.0 --port 8000
Open http://localhost:8000.

## API
- GET /api/health — service health
- GET /api/status — selected market, report count and data readiness

## Deployment
Render build command: pip install -r requirements.txt
Render start command: uvicorn app:app --host 0.0.0.0 --port $PORT

## Principles
- Free data sources where possible; source provenance and file hashes retained.
- No fabricated or stale data presented as current.
- Open interest alone does not identify who bought or sold.
- Forecasts are frozen before the next session and scored without hindsight.
- Directional accuracy is distinct from options trading profitability.
- Research support only, not financial advice.

See docs/ARCHITECTURE.md for the architecture.
