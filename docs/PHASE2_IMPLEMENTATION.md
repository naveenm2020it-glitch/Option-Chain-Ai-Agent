# Phase 2 — Data normalization and feature dataset preparation

## Scope

- NIFTY 50 and NIFTY index options only.
- Consume the existing schema-v3 historical feature checkpoint produced by the read-only historical audit.
- Prepare four chronological research datasets: direction, range, CE seller premium, and PE seller premium.
- Do not train models, place orders, call live-market APIs, or write to Backblaze B2.

## Existing Phase 1 interface

The input is the historical audit checkpoint JSON:

`data/historical_audit/checkpoint.json`

It must contain exactly the requested number of session records under `features`. Each session record must include:

- `session_date`
- `feature_schema_version = 3`
- finite feature values
- contract snapshots keyed by expiry, strike, and option type

The existing `scripts/nse_ml_features.py` remains the authoritative schema-v3 extractor.

## Dataset builder

Run:

```bash
python scripts/prepare_nifty_training_dataset.py \
  --checkpoint data/historical_audit/checkpoint.json \
  --output-dir data/prepared_models \
  --expected-sessions 494
```

Outputs:

- `direction/{train,validation,holdout}.jsonl`
- `range/{train,validation,holdout}.jsonl`
- `ce_seller/{train,validation,holdout}.jsonl`
- `pe_seller/{train,validation,holdout}.jsonl`
- `manifest.json`
- `quality_audit.json`

## Controls

1. Schema version and session-date alignment are mandatory.
2. Required cash-index, futures, participant OI and participant-volume context availability is fail-closed.
3. Cash OHLC ordering and derived range/close-location values are recomputed and checked.
4. All numeric features must be finite; duplicate contract keys and expired contracts are rejected.
5. A 22-session warm-up is required. Rolling windows are 5 and 22 sessions.
6. Splits are chronological by target date: 70% train, 15% validation, 15% holdout. No shuffle.
7. Seller targets use exact expiry + strike + option type matches, require current moneyness within 3%, positive OI/volume, premium >= 5 points, and at least five contracts. Volume-weighted median premium return is used.
8. Next-session observations are targets only; they are never included in feature windows.
9. Forecast timestamp is explicitly after the current session close. This dataset must not be used to claim an intraday signal.
10. Delta availability remains represented by a mask. Missing delta is not fabricated.
11. Participant positioning is broad NSE index-derivatives context, not proof of NIFTY-only seller identity.
12. OI/premium combinations are seller-flow proxies, not proof of who opened a position.
13. Seller target metadata records the selected expiry, exact matched-contract count, and calendar days remaining to that expiry on the target date.
14. Large expiry-day premium moves are retained as observed outcomes, but are reported separately; they are not silently clipped or discarded.
15. The current +/-1% seller label band is provisional, not calibrated. The audit reports sensitivity at multiple bands, but no band is selected from validation/holdout results in Phase 2. Revisit economic labels and transaction-cost assumptions before supervised training.

## Completion boundary

Passing the synthetic regression suite validates code paths and invariants, not the live historical checkpoint. Phase 2 dataset production is complete only after the full 494-session checkpoint is run through the builder and its output manifest is reviewed. Model training remains disabled.


## CI note
The validation workflow installs dependencies explicitly and does not depend on a repository requirements file.

The Phase 2 CI workflow also runs the builder against the previously verified 494-session checkpoint and publishes the resulting JSONL research datasets as a GitHub Actions artifact. No model training is invoked.

The build pins the byte-for-byte SHA-256 of the selected historical checkpoint artifact before processing, so an altered or mismatched artifact fails closed.

The CI quality audit verifies all 12 JSONL files against the manifest, checks finite feature values, 182-feature consistency, unique chronological targets, split separation, after-close markers, and seller target metadata. It reports per-split class counts, target quantiles, seller-label sensitivity at 0.5%, 1%, 2%, and 3%, expiry-horizon distributions, and extreme premium-return concentration. Quality warnings do not silently alter labels or block artifact publication; they keep model training marked for review.

Each exported JSONL sample carries `forecast_as_of: after_close` as an explicit row-level field as well as in the dataset manifest.
