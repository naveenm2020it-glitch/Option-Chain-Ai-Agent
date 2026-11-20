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


## Final integrity gate (added 2026-11-17)
Before System 1 can be accepted by Systems 2–8, the selected-session package must pass all of these checks:
- NIFTY option rows must carry a parseable trade date equal to the requested session.
- Expiry values are parsed and canonicalized to ISO dates; expired, malformed or future-invalid contract dates are rejected.
- Strike must be positive; close, OI and volume must be non-negative finite numbers; change-in-OI may be negative.
- Both CE and PE must be present in every session package; duplicate (expiry, strike, option type) keys are a hard failure.
- The 23-session window must have 23 ready sessions, not merely a ready selected date.
- A second package build from identical manifests must produce the same stable SHA-256 fingerprint (generation timestamp excluded).
- Regression tests cover mismatched dates, invalid expiry, negative OI, CE/PE completeness and frozen fingerprint stability.

The final workflow runs the System 1 regression suite before re-downloading and validating the 23-session NSE source window. This remains a pre-merge validation branch; it does not merge the PR, train models, call B2, or use Render.

## Evidence boundaries
The configured NSE report audit checks file integrity, table structure, date evidence where exposed, and selected report-specific ZIP member requirements. Generic content validation is not proof that every optional report field has been economically reconciled. Until report-specific field mappings and reconciliation tests are added for an optional report, downstream engines must treat it as contextual/availability-marked input rather than a fully reconciled feature source. IV/Greeks/bid-ask/intraday remain unavailable unless separately verified.


## Backblaze B2 archive (implementation in progress)
- The full 23-session audit can optionally upload one deterministic `.tar.gz` object containing the raw report files, per-session manifests, frozen normalized package and archive manifest.
- Upload is gated on all 23 sessions, all configured reports, normalized CE/PE/expiry checks and frozen-package reproducibility passing.
- Object key: `system1/archives/windows/{analysis_date}/{package_fingerprint}.tar.gz`.
- The uploader uses the B2 S3-compatible endpoint with `boto3`. It checks the immutable object key first; matching content is treated as already present, while different content at the same key is rejected rather than overwritten.
- Required GitHub Actions secrets: `B2_APPLICATION_KEY_ID`, `B2_APPLICATION_KEY`, `B2_BUCKET_NAME`, `B2_ENDPOINT_URL`. Optional: `B2_REGION`.
- B2 upload is enabled by the manual workflow's `archive_to_b2` input (default true) and only runs for a 23-session audit. One new archive upload costs one B2 HEAD plus one PUT; a repeat identical archive costs one HEAD.
- The validator now reads the fixed rolling object before collection, reuses cached report files only when their SHA-256 matches, fetches missing/corrupt reports only, prunes sessions outside the requested 23-session window, and writes the refreshed rolling snapshot after all gates pass.
- Daily cache path uses one B2 GET plus one B2 PUT for the rolling object (two object operations); a first run has one GET cache-miss plus one PUT. It avoids per-report B2 calls and avoids re-downloading the 22 overlapping sessions when the cached archive is intact.
- This cache path is implemented but remains unverified against the user's live B2 bucket until the manual 23-session workflow is run with valid secrets. The GitHub Actions push trigger runs regression tests only; it deliberately does not launch the expensive NSE audit.
- The workflow is manual-only to avoid accidental repeated 23-session downloads on every code push. It does not deploy to Render or start model training.
