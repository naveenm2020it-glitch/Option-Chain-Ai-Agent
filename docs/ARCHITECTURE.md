# Architecture — NIFTY 50 Next-Day Intelligence

## Fixed scope
- Primary index: NIFTY 50
- Derivatives: NIFTY index options only
- Delivery: mobile-first web dashboard, hosted in the cloud
- Data policy: free sources first; every report carries source, report date, fetch time, checksum and parse status.

## Pipeline
1. Source discovery: enumerate official NSE report types and published endpoints/archives. Do not guess URLs or scrape undocumented endpoints without checking access and terms.
2. Collection: scheduled job downloads reports; retries transient failures; validates raw originals and writes a SHA-256 manifest. Missing files remain missing, never silently replaced by stale data.
3. Durable archive: upload one ZIP of the daily collection plus its manifest to a private Backblaze B2 bucket using its S3-compatible API. Use date-based object keys and a catalog index. The collection date is deliberately separate from each report's own report_date; publication date must not be mislabeled as trading session date. Re-running a date is idempotent.
4. Normalization: parse cash OHLC/volume, F&O bhavcopy, option-chain snapshots where publicly available, participant OI/volume and FII/DII reports into typed tables.
5. Quality gates: validate schema/columns, duplicate detection, row counts, plausible numeric ranges and source freshness. The current fail-closed session gate requires the F&O UDiFF bhavcopy, participant OI, participant volume and Market Activity archive to have validated files and exact same report_date. This is a provisional gate and must be corrected to compare semantic trading-session dates with report-family publication lags before live prediction is enabled. Missing files, invalid status, missing dates or mixed dates block analysis.
6. Analytics: price + OI + volume + premium changes, PCR, strike concentration, volatility, participant positioning and support/resistance. OI alone never proves buyer/seller identity. Filter derivatives to NIFTY contracts before calculating NIFTY-only signals.
7. Forecast freeze: save a timestamped next-session forecast with input-data manifest hash before the next session. Never revise a frozen forecast.
8. Scoring: compare the frozen forecast with the next session realized result using chronological walk-forward evaluation. Directional accuracy and option strategy P&L are separate metrics.

## API
- `/api/status`: local data readiness and whether B2 credentials are configured.
- `/api/history`: reads the B2 collection catalog. It does not yet download or parse historical ZIP contents.

## Configuration
Set `B2_ENDPOINT_URL`, `B2_APPLICATION_KEY_ID`, `B2_APPLICATION_KEY`, and `B2_BUCKET_NAME` as GitHub Actions secrets for the collector and as environment variables for the hosted app. Never commit credentials.

## Deployment
Render build command: `pip install -r requirements.txt`
Start command: `uvicorn app:app --host 0.0.0.0 --port $PORT`
