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

## Verified one-date sample
On 2026-11-17, GitHub Actions downloaded and parsed the official NSE reports for 2026-01-07:
- F&O UDiFF Bhavcopy: HTTP 200, 1,186,613 bytes, SHA-256 recorded.
- NIFTY index close report: HTTP 200, 15,234 bytes, SHA-256 recorded.
- F&O source rows: 39,202; NIFTY CE/PE contracts extracted: 1,484.
- Duplicate contract keys: 0.
- NIFTY 50 close: 26,140.75; India VIX close: 9.95.
- Package status: READY; unit tests passed.

The sample reports were held only in the ephemeral GitHub Actions runner and were not committed to the repository. The manually triggerable workflow is `.github/workflows/system1-sample-test.yml`.
