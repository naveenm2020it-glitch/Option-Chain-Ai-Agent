# NIFTY Adaptive Intelligence V2

## Execution order

1. The scheduled/manual historical audit reads the B2 catalog once per run.
2. It downloads at most 25 existing session ZIPs per run.
3. Each downloaded ZIP is checked against catalog byte count and SHA-256, ZIP CRC, embedded manifest, session date, and individual report hashes.
4. The same in-memory ZIP bytes are used to build NIFTY features. No second B2 download is required for feature extraction.
5. Checkpoints are saved after every archive and restored from GitHub Actions cache on the next run.
6. A B2 Class B quota error pauses the run without retry loops or archive writes.
7. A single missing calendar-confirmed session may be repaired by the existing backfill script; the new ZIP is re-audited before the training gate can open.
8. Model training starts only when all 494 expected sessions are integrity-verified, all 494 NIFTY feature rows are parsed, and the required data-source coverage checks pass.

## Feature sources

- NSE UDiFF FO bhavcopy: NIFTY index options and index futures (IDO / IDF).
- Near-expiry and up-to-45-day option-chain aggregates: CE/PE OI, change in OI, volume, premium changes, seller-flow proxies, moneyness and expiry.
- NSE participant-wise OI and volume: FII, DII, Client and Pro index futures/options positioning. These are NSE-wide index-derivatives totals, not NIFTY-only positions, and are treated as contextual features.
- NSE index close report: NIFTY 50 OHLC and India VIX when present.
- Derived 1-day, 5-session and 20-session features.

OI + premium combinations are seller-flow proxies. They do not identify the actual side of every trade and are not treated as proof of writing.

## Training and validation

- Direction: CatBoost vs LightGBM, compared with expanding chronological walk-forward folds.
- Probabilities: time-series sigmoid calibration; an uncalibrated fallback is not marked deployment-ready.
- Range: quantile regression for next-session close, upper and lower range.
- Event risk: large-move, breakout and false-breakout/trap classifiers.
- Seller outcomes: separate CE and PE contract-level regressors trained on matched contracts across adjacent sessions.
- Regime and historical analogs: Gaussian-mixture regime representation and nearest-neighbour similarity index.
- Validation: chronological 80/20 holdout, macro-F1, multiclass Brier score, baseline comparison, quantile pinball loss and seller-model MAE.

The trained model is a research candidate. It is marked deployment-ready only if it beats the baseline by the configured holdout criteria, has calibrated probabilities, and both CE/PE seller models have sufficient samples. Otherwise the artifact is retained as a non-deployable candidate and the report explains why.

## Artifacts

A successful training run uploads:
- `nifty_adaptive_v2.joblib`
- `model_manifest.json`

The artifact is not automatically loaded by Render or connected to the existing UI/API in this pipeline. UI and live backend behaviour remain unchanged until a separately validated integration is approved.
