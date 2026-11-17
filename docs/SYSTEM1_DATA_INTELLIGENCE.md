# System 1 — Data Intelligence

## Purpose
System 1 is the single ingestion/validation boundary for Systems 2–8. It emits one date-stamped package; downstream systems consume this frozen package and do not independently fetch NSE data.

## Engine mapping
1. NSE Data Collection — existing scripts/nse_download_check.py remains the downloader. This new validator does local reads only.
2. Option Chain Data — parse UDiFF ZIP/CSV and normalize NIFTY CE/PE expiry, strike, close, OI, change in OI and traded volume.
3. Historical Archive — selected date plus 22 previous exchange sessions (23 total); one-session diagnostic supported.
4. Data Quality — manifest, file, safe-path, SHA-256, ZIP, CSV, required-column, numeric and duplicate-key checks.
5. Data Reconciliation — exact selected-session alignment for mandatory NIFTY option UDiFF and NIFTY index close. Other report families remain optional context until publication-date semantics are verified.
6. Data Freshness — preserve report date and collection metadata; never silently substitute another date.

## Mandatory inputs
- fo_udiff_bhavcopy: NIFTY CE/PE contracts from NSE F&O UDiFF Common Bhavcopy Final.
- nifty_index_close_all: NIFTY 50 index close from NSE indices daily close report.

Optional context: CM bhavcopy, participant OI/volume, market activity, settlement prices, daily volatility, contract master and contract delta. Participant reports are broad participant context, not proof of NIFTY-specific writers.

## Output
JSON includes schema version, analysis date, session window, per-session report status, normalized NIFTY option rows, source hashes, row counts, blocked-session list and limitations. Missing data is never zero-filled.

## Commands
One-date diagnostic:
    python scripts/system1_validate.py --date 2026-01-07 --window-sessions 1 --reports data/nse_reports

Full window (selected date plus 22 previous sessions):
    python scripts/system1_validate.py --date YYYY-MM-DD --window-sessions 23 --reports data/nse_reports --output data/system1/package.json

The full window expects date folders containing manifest.json. A root manifest is accepted when its core session matches the requested date.

## Operating limits
- Validation is local only: no NSE, B2, PostgreSQL or Render calls.
- No historical backfill, model training, dashboard changes or deployment.
- Test one sample date first; expand to 23 sessions only after it passes.
- IV, Greeks, bid/ask and intraday values are not fabricated or presumed available.
