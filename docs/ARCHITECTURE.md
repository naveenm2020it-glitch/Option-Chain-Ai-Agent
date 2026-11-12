# Architecture — NIFTY 50 Next-Day Intelligence

## Fixed scope
- Primary index: NIFTY 50
- Derivatives: NIFTY index options only
- Delivery: mobile-first web dashboard, hosted in the cloud
- Data policy: free sources first; every report carries source, trading date, fetch time, checksum and parse status.

## Pipeline
1. Source discovery: enumerate official NSE report types and published endpoints/archives. Do not guess URLs or scrape undocumented endpoints without checking access and terms.
2. Collection: scheduled job downloads reports; retries transient failures; stores raw originals and SHA-256 manifest. Missing files remain missing, never silently replaced by stale data.
3. Normalization: parse cash OHLC/volume, F&O bhavcopy, option-chain snapshots where publicly available, participant OI/volume and FII/DII reports into typed tables.
4. Quality gates: trading-date alignment, schema/column checks, duplicate detection, row counts, plausible numeric ranges and source freshness.
5. Analytics: price + OI + volume + premium changes, PCR, strike concentration, volatility, participant positioning and support/resistance. OI alone never proves buyer/seller identity.
6. Forecast freeze: save a timestamped next-session forecast with input-data manifest hash before the next session. Never revise a frozen forecast.
7. Scoring: compare the frozen forecast with the next session realized result using chronological walk-forward evaluation. Directional accuracy and option strategy P&L are separate metrics.

## First implementation
The current commit provides a FastAPI service, mobile dashboard shell, health/status endpoints and CI smoke tests. It intentionally publishes no market prediction while live data is absent.

## Deployment
Render build command: pip install -r requirements.txt
Start command: uvicorn app:app --host 0.0.0.0 --port $PORT
Configure persistent storage or external durable object storage before treating local filesystem as an archive. Never commit credentials.
