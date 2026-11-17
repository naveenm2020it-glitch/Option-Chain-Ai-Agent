# Historical Feature Readiness Audit — 2026-11-17

## Scope and evidence

This audit cross-checks two GitHub Actions artifacts:

- Archive-integrity checkpoint and coverage from run `44007493279`.
- Enriched context checkpoint and raw context CSV sidecars from run `44008350973`.

The enriched context repair reused already-present GitHub artifact data. It reports **0 B2 requests**, 0 new NSE downloads, and 494/494 already-present records for each context family. This is not a fresh live B2 bucket listing.

## Final readiness result

**Historical feature data: PASS for research dataset preparation.  
Model training: not started.**

| Check | Result |
|---|---:|
| Window | 2024-11-12 to 2026-11-12 |
| Expected / verified sessions | 494 / 494 |
| Failed sessions / feature errors | 0 / 0 |
| Feature schema | v3 |
| Feature fields per session | 246 / 246 |
| NIFTY option contract snapshots | 159,036 |
| CE / PE contract rows | 79,518 / 79,518 |
| Duplicate date-expiry-strike-option keys | 0 |
| Expired contracts included | 0 |
| Null / non-finite feature values | 0 / 0 |
| NIFTY options / futures availability | 100% / 100% |
| NIFTY cash OHLC availability | 100% |
| India VIX availability | 100% |
| Participant OI / volume availability | 100% / 100% |
| Contract delta availability | 1 / 494 (optional gap) |

The 494 feature dates match the verified-date keys and each row's `session_date`. All sessions have CE and PE contracts. Contract expiry is not earlier than the session date. The catalog snapshot includes one additional date, 2026-11-13, outside the feature window; it is not counted as an audited feature session.

## Context source provenance

The context-repair report records:

- Index OHLC: 494/494 available; 493 report CSVs independently SHA-256 verified and one source already present in the integrity-verified archive.
- Participant OI: 494/494 available; 494 CSV SHA-256 checks passed.
- Participant volume: 494/494 available; 494 CSV SHA-256 checks passed.
- No missing context dates, invalid reports, date-alignment errors, or SHA-256 mismatches.
- VIX close is populated for all 494 sessions (values 9.15 to 27.89 in the checkpoint).
- Contract delta is matched for only one session; keep its availability mask and do not fabricate it.

Participant positioning reports are broad NSE index-derivatives participant totals, not proof of NIFTY-only participant positions. Use them as contextual confirmation, not as direct NIFTY seller identity. OI/premium combinations remain seller-flow proxies, not proof of who opened a position.

## Defect found and fixed

The earlier enriched checkpoint contained real OHLC values, but its derived `index_range_pct` and `index_close_location` were stale zero placeholders for most sessions because the context-repair merge did not recompute them.

The fix now:
1. Recomputes both derived fields after the OHLC context is merged.
2. Restores the `index_close` alias from the already-selected `spot_close` only for an older row whose index availability flag is true but whose checkpoint omitted that field.
3. Validates OHLC ordering and exact derived-feature consistency before passing the context-repair gate.
4. Stops the original extractor from substituting the close price for missing open/high/low. Unavailable OHLC stays explicitly unavailable.

The newly published artifact from run `44008350973` was re-inspected:
- 494/494 rows have 246 feature fields.
- 494/494 have valid OHLC ordering.
- 494/494 have recomputed `index_range_pct` and `index_close_location`.
- No nulls, non-finite values, stale derived fields, or feature-validation errors.
- All 494 index, participant OI and participant volume source records were already present; 0 were downloaded during this run; B2 requests = 0.

## Archive diagnostic / B2 note

The earlier archive audit artifact includes a diagnostic for 2026-10-05. Its embedded manifest and all six report-file hashes validated, but catalog archive hash/byte metadata differed. That run recorded `catalog_repaired: true` and `b2_writes: 1`; the repair basis was the embedded manifest and report SHA-256 checks. This was one catalog metadata write, not a rewrite of the raw report ZIP.

The subsequent context repair and feature recalculation made **0 B2 requests**. Do not describe the entire historical process as having made zero B2 writes.

## Training gate and next steps

The data is ready to enter model-dataset preparation, but this does not certify a model or its predictive performance.

Before training:
1. Build explicit next-session targets from the next trading session only; freeze target definitions and align the special Muhurat session correctly.
2. Exclude current-session `future_return` from any feature set if the forecast timestamp precedes that session's close. It is only a valid input for an after-close forecast.
3. Use chronological train/validation/holdout splits; no random split.
4. Add leakage tests, target-availability tests, missingness-mask tests and baseline comparisons.
5. Keep delta availability as a mask; do not impute it.
6. Treat participant index-derivatives positioning as broad context, not NIFTY-specific seller identity.
7. Train the four new models only after dataset-preparation tests pass. No training or tuning was triggered by this audit.

