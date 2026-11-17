# Historical Feature Readiness Audit — 2026-11-17

## Scope and method

This is an offline inspection of the GitHub Actions artifact `nifty-historical-audit-44007493279` produced on 2026-11-15. The artifact's checkpoint and coverage report were inspected locally. No B2 API calls, NSE downloads, historical backfill, model training, or workflow dispatch were performed for this inspection.

This report describes the checkpoint snapshot, not a fresh verification of the live B2 bucket.

## Summary

**Core archive/feature coverage: PASS in the supplied checkpoint.  
Enriched seller-model readiness: BLOCKED pending context-data recovery and OHLC correction.**

| Check | Result |
|---|---:|
| Window | 2024-11-12 to 2026-11-12 |
| Expected sessions | 494 |
| Verified sessions | 494 |
| Failed sessions | 0 |
| Feature rows | 494 |
| Feature extraction errors | 0 |
| Feature schema | v3 for all 494 rows |
| Features per session | 65 |
| NIFTY option contract snapshots | 159,036 rows |
| CE / PE contract rows | 79,518 / 79,518 |
| Duplicate (date, expiry, strike, option type) keys | 0 |
| Null / non-finite feature values | 0 |
| NIFTY option availability | 100% |
| NIFTY futures availability | 100% |

The 494 feature dates match the verified-date keys and each row's `session_date`. Contract expiries precede no session dates. Each date has both CE and PE contracts. The archive catalog snapshot contains 495 dates, including 2026-11-13 outside the audited feature window; this is not counted as one of the 494 feature sessions.

## Blocking feature gaps

| Feature family | Coverage | Finding |
|---|---:|---|
| NIFTY cash index OHLC | 1 / 494 (0.20%) | Missing for 493 sessions |
| India VIX close | 1 / 494 (0.20%) | Missing for 493 sessions |
| Participant OI | 0 / 494 | Unavailable for all sessions |
| Participant volume | 0 / 494 | Unavailable for all sessions |
| Contract delta | 1 / 494 (0.20%) | Missing for 493 sessions |
| NIFTY options | 494 / 494 | Available |
| NIFTY futures | 494 / 494 | Available |

### OHLC fallback defect

In `scripts/nse_ml_features.py`, when the index-close report is unavailable, `spot` falls back to the median NIFTY option underlying price. The exported `spot_open`, `spot_high`, and `spot_low` then also fall back to that same `spot` value.

As a result, 493 rows have synthetic flat OHLC values (open = high = low = close) and zero `index_range_pct` / `index_close_location`. These are not genuine cash-index OHLC observations. They must remain explicitly unavailable, not be treated as market range data.

### Participant and delta parsing

The feature extractor sets participant availability only when it recognizes FII, DII, Client and Pro rows and all required index-derivative long/short columns. The checkpoint marks both participant feeds unavailable for every session. Delta matching is also unavailable for 493 sessions.

The checkpoint alone cannot distinguish whether these gaps are caused by absent report files, changed NSE report headers, or parser mismatch. Do not infer the cause without inspecting representative raw report manifests and CSVs. That inspection must be planned as a small, bounded sample and must avoid a full B2 re-read.

## Archive diagnostic note

The audit artifact includes a diagnostic for 2026-10-05. Its embedded manifest and all six report-file hashes validated, but the catalog archive hash/byte metadata differed. The audit recorded `catalog_repaired: true` and `b2_writes: 1`, with the repair basis recorded as the embedded manifest and report SHA-256 checks. This was one catalog metadata write; it was not a rewrite of the raw report ZIP. Do not describe the earlier audit as having made zero B2 writes.

## Training gate

- Core option/futures historical feature baseline: **PASS** for exploratory research and pipeline tests.
- Enriched seller-intelligence training: **BLOCKED** until the cash OHLC/VIX coverage issue is corrected or explicitly excluded with robust missingness handling, participant reports are parsed or formally marked optional, and delta is either recovered or excluded.
- No model training, tuning, backfill, or prediction is authorized by this audit.
- Any eventual evaluation must use chronological splits, frozen next-session targets, and leakage checks. Same-session `future_return` is an input only if the prediction timestamp is after that session's close; it must not be used as a next-session target.

## Low-call next steps

1. Inspect one representative raw archive manifest and participant/index CSV for a date with missing features, using the smallest possible read-only sample.
2. Fix header/date parsing or report collection only after confirming the root cause.
3. Rebuild feature rows from already archived source bytes where available; do not redownload the full 494-session range.
4. Add tests ensuring missing OHLC stays missing (never copied from close) and that optional-source availability flags are truthful.
5. Re-run the offline checkpoint audit. Only then prepare the four-model training dataset.

