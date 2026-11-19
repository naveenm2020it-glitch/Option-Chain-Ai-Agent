# Phase 2 — NIFTY EOD normalization and feature pipeline

## Scope
Phase 2 converts the isolated Phase 1 NSE archive into deterministic, typed CSV tables and a dataset manifest. This is data engineering only: no trade signals, model fitting, or order placement.

```bash
python scripts/normalize_eod_dataset.py --archive /path/to/phase1-artifact.zip --output ./normalized-eod --expected-sessions 22
```

The pipeline uses only Python's standard library and expects the Phase 1 artifact layout, including `reports/trial-manifest.json` and one dated FO Bhavcopy ZIP per aligned session.

## Outputs
- `nifty_options_eod.csv`: all NIFTY CE and PE rows across strikes and expiries. Zero-volume rows are retained. Includes OHLC, close, previous close, underlying price, OI, OI change, volume, value, trade count, lot size, and contract-delta-factor field.
- `nifty_index_eod.csv`: NIFTY 50 OHLC, close, points/percent change, volume, turnover and valuation fields.
- `participant_positioning_eod.csv`: participant-wise market-wide derivatives aggregates. These are not NIFTY-only contract positions.
- `session_feature_summary.csv`: daily CE/PE counts, aggregate OI/change/volume, index range/change, and participant-report availability.
- `dataset_manifest.json`: source SHA-256, sessions, row counts, per-session CE/PE coverage, missing reports, lineage and limitations.

## Data rules
1. FO Bhavcopy is the authoritative current-session contract universe. All 22 Phase 1 contract-master files were semantically stale and are excluded.
2. Only `TckrSymb=NIFTY` and `OptnTp` CE/PE are included. Strikes remain as NSE-reported; no 100x scaling.
3. Contract comparisons use expiry/strike/type and prior available session. New contracts receive null baselines, not fabricated values.
4. NSE `Delta Factor` is retained as `contract_delta_factor`; it is not option Greek delta.
5. Participant reports are market-wide, not NIFTY-specific seller positions.
6. EOD snapshots cannot establish intraday order, trigger crossings, or actual seller entry premiums.
7. All 22 aligned sessions and both CE/PE sides are required by default. Missing optional reports are recorded, not imputed.

## Validation gate
Exact session count; CE and PE in every session; unique session/instrument keys; separately normalized index and participant reports; row counts recorded in manifest; unit tests for parsing, schema and incomplete input. Model training and predictive claims remain out of scope pending a leakage-safe walk-forward design.
