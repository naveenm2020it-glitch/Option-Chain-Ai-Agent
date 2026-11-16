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


## Seller Intelligence levels engine

- `POST /api/engine/levels` accepts a normalized, same-session NIFTY option-chain snapshot.
- PE-side OI concentration and confirmed OI addition with non-rising premium are scored as support candidates.
- CE-side equivalents are scored as resistance candidates.
- OI reduction and OI addition alongside a rising option premium are surfaced as seller exit/pressure-risk zones.
- Scores combine OI concentration, change in OI, volume and premium confirmation. Missing inputs reduce confidence; missing premium data cannot be labelled confirmed writing.
- The engine fails closed unless `data_quality.aligned=true`, a `session_date`, and at least three valid strikes are supplied.
- These are positioning proxies, not proof of participant identity or guaranteed defended levels. Price reaction is required for confirmation.
- The current repository does not yet parse raw NSE archives into a normalized daily option-chain snapshot, so the API is implemented but live dashboard levels remain unavailable until that adapter is completed. No extra B2 archive reads or writes are introduced by this engine.


## Daily seller-level context and rolling persistence

The daily NSE workflow builds a normalized seller snapshot from the already-downloaded, same-session UDiFF F&O bhavcopy. It reuses the retained 494-session checkpoint artifact for historical context; it does not query Backblaze B2.

- The option adapter uses the front unexpired NIFTY index-option expiry for each session and aggregates CE/PE OI, change-in-OI, volume and premium movement by strike.
- Premium confirmation is unavailable when the contract has no valid previous close; a missing baseline is never treated as a zero premium return.
- PE writing candidates contribute to support candidates; CE writing candidates contribute to resistance candidates. OI unwinding and OI-addition with premium expansion are surfaced as exit/squeeze-risk flags.
- Last 5 and last 22 completed sessions are summarized by strike persistence and average strength. The current session is excluded from both historical windows.
- The collector posts the daily manifest and compact seller context together to the existing authenticated daily-ingest endpoint. PostgreSQL stores the latest context in the existing JSON cache table. The dashboard reads it through GET /api/seller-intelligence/latest.
- If PostgreSQL ingestion is not configured or the daily adapter fails, the dashboard reports seller context as unavailable; it does not fabricate levels.
- No extra B2 reads/writes, historical re-backfill, or automatic trading are introduced.

All levels remain evidence-weighted positioning proxies. OI does not identify the participant who opened a position, and price reaction is required before calling a level defended.
