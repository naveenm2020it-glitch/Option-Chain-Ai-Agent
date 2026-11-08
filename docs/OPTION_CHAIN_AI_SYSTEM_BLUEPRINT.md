# NIFTY Option Chain AI — Product & Technical Blueprint

**Status:** Design baseline; implementation not yet completed  
**Date:** 2026-11-08  
**Repository:** `naveenm2020it-glitch/Option-Chain-Ai-Agent`

## 1. Product outcome

Build a NIFTY-only options research and option-buying decision-support terminal. The decision engine must use option-chain snapshots, CE/PE LTP, Greeks, OI and change-in-OI together. Premium candles are NOT required for selecting the option, calculating the entry/target/stop levels, or deciding the market direction. It must preserve every AI analysis and every signal lifecycle event so performance can be measured from observed option-chain LTP updates.

This is an analytical tool, not an order-placement system. No broker login, broker API, automated order placement, paid data feed, API-key-only data source, fabricated market values or unverified live status.

### User's fixed trade parameters

- Instrument: NIFTY 50 options only.
- Strategy: long option buying (CE or PE).
- Minimum target: +20 option-premium points from actual entry fill/trigger price.
- Stop-loss: -10 option-premium points from actual entry fill/trigger price.
- Evaluation horizon: 15 minutes from confirmed entry.
- Risk/reward at the minimum configured levels: 2:1 before costs and slippage.
- If evidence is mixed, data is stale, the spread is too wide, or the option-chain LTP/quote is missing: WAIT; do not alert.

These are signal evaluation parameters, not a promise that a target will be reached.

## 2. User workflow

1. Verify source authorization, connection, timestamps and data quality.
2. Fetch one coherent NIFTY spot + expiry + CE/PE chain snapshot.
3. Normalize and persist the raw snapshot and normalized contracts.
4. Calculate or validate Greeks for every contract.
5. Calculate Max Pain, PCR, OI concentration, OI build-up/unwinding and dynamic OI zones.
6. Combine option-chain positioning, CE/PE LTP movement, Greeks, OI and change-in-OI. Use spot value/context from the same valid snapshot where available; do not require premium candles.
7. Determine BULLISH / BEARISH / SIDEWAYS / WAIT and explain evidence for and against the view.
8. Rank candidate CE/PE contracts; validate liquidity, spread, IV and premium behaviour.
9. Calculate an entry trigger from the selected option's observed LTP and the combined chain/Greek/OI evidence. Move a candidate through an explicit signal state machine. Only a confirmed option-chain snapshot trigger can become an entry alert.
10. Track target, stop, time expiry, invalidation and missing-data outcomes using subsequent valid option-chain LTP observations for the exact selected contract.
11. Persist every analysis, including WAIT and no-trade decisions.
12. Display performance and export the complete history to Excel.

## 3. Data-source boundary and safety gate

The provider adapter must be independent from the UI and analytics. A provider can be enabled only after its terms permit the intended automated use and its response can be validated.

The previous source audit found no verified compliant free/no-key/non-broker automated NIFTY option-chain provider satisfying all constraints. The official NSE MCP does not advertise derivatives option-chain tools. Unofficial NSE website endpoint wrappers are not automatically authorized feeds. Do not use undocumented NSE website endpoints for continuous automated polling absent permission. Do not add rotating browser fingerprints, CAPTCHA bypass, cookie replay, proxy rotation or access-control evasion.

Until an approved provider is configured:
- API status remains `UNAVAILABLE` (or `STALE` for a retained last-good snapshot).
- Live analysis, live alerts and performance claims remain disabled.
- No synthetic/demo market values are shown as real.
- Deterministic unit tests may use clearly labelled test fixtures, isolated from production endpoints and UI.

A successful HTTP response alone does not qualify as LIVE. Validate source timestamp progression, snapshot age, selected expiry, spot presence, strike uniqueness, CE/PE fields, numeric ranges and cross-field consistency.

## 4. Proposed architecture

```text
Permitted market-data provider
          |
          v
Provider adapter (one adapter per source)
          |
          v
Raw response + source timestamp + received timestamp
          |
          v
Normalizer (spot / expiry / strikes / CE / PE)
          |
          v
Quality gate (freshness, schema, duplicates, coherent snapshot)
          |
          +------> Provider health + last-good snapshot
          |
          v
SQLite persistence (snapshots, chain rows, candles, analyses, signals)
          |
          +------> Greeks engine
          +------> OI / PCR / Max Pain engine
          +------> CE/PE LTP + Greek/OI feature engine
                         |
                         v
               Direction + setup evaluator
                         |
                         v
                Signal state machine
                         |
             +-----------+-----------+
             v                       v
        FastAPI JSON/SSE        Outcome tracker
             |                       |
             v                       v
         Dashboard UI          Performance / Excel
```

### Suggested module boundaries

- `providers/base.py`: provider protocol and capability declaration.
- `providers/<provider>.py`: only approved source integration.
- `services/normalizer.py`: stable internal schema.
- `services/data_quality.py`: source freshness and field validation.
- `services/greeks.py`: Black–Scholes calculations and input provenance.
- `services/oi_analytics.py`: PCR, Max Pain, OI rankings and changes.
- `services/direction.py`: transparent multi-factor directional evidence.
- `services/setup_engine.py`: CE/PE candidate and liquidity checks.
- `services/signal_lifecycle.py`: trigger, target, SL, timeout and invalidation.
- `services/outcomes.py`: outcome tracking from successive validated option-chain LTP observations for the exact contract; no premium candle dependency.
- `storage/database.py`: SQLite schema and repository methods.
- `services/export.py`: multi-sheet Excel export.
- `api/...`: versioned JSON endpoints and optional SSE.

Keep Python 3.12 compatibility and a low-CPU footprint. SQLite is sufficient for a single-user Render/local deployment at this stage. Use a single collector loop and request coalescing only for an authorized provider; do not run one upstream request per browser client.

## 5. Normalized data contract

Top-level snapshot:

```json
{
  "status": "UNAVAILABLE",
  "source": null,
  "source_timestamp": null,
  "received_at": null,
  "exchange_timezone": "Asia/Kolkata",
  "instrument": "NIFTY",
  "spot": null,
  "expiry_dates": [],
  "selected_expiry": null,
  "rows": [],
  "quality": {
    "age_seconds": null,
    "is_fresh": false,
    "checks": [],
    "warnings": []
  },
  "error": "No permitted live provider has been configured and verified."
}
```

Each chain row contains strike, expiry and CE/PE legs. Each leg preserves raw provider fields where available: LTP, change, percent change, volume, OI, change in OI, IV, bid/ask price and quantity. Missing fields remain null; do not silently replace them with zero. Keep raw source values and calculated values distinguishable.

A snapshot ID must link spot, all option contracts, Greeks, analytics and candles to the same observation window. Never combine CE and PE or spot values from different expiries/snapshots and present them as one coherent state.

## 6. Greeks calculation

Display Delta, Gamma, Theta, Vega and Rho for CE and PE.

Preferred input order:
1. Use provider-supplied Greeks when the provider documents their calculation and timestamp.
2. Otherwise calculate from a documented European Black–Scholes model using spot, strike, time-to-expiry, risk-free rate, dividend yield and IV.
3. If IV or required inputs are unavailable, return null with a reason. Do not invent defaults silently.

Persist:
- model name and version;
- option type, spot, strike, expiry timestamp;
- IV and whether it is provider-supplied or derived;
- rate and dividend assumptions;
- time-to-expiry convention;
- calculation timestamp and units.

Use a configurable risk-free-rate input with its source/date recorded. Theta units must be explicit (per calendar day or per year); Vega and Rho units must also be labelled. Add numerical tests for put-call parity and finite-difference checks.

## 7. OI and Max Pain analytics

For the selected expiry calculate:
- total CE OI and PE OI;
- total and strike-wise change in OI;
- OI PCR = total PE OI / total CE OI;
- volume PCR separately, when volume is present;
- top CE OI and top positive CE change-OI strikes;
- top PE OI and top positive PE change-OI strikes;
- unwinding leaders (negative change in OI);
- Max Pain using intrinsic payout weighted by current OI;
- support/resistance zones from clustered Put/Call OI and change-OI evidence.

Do not label a single top Put OI strike as guaranteed support or a single top Call OI strike as guaranteed resistance. Track whether zones strengthen, weaken or shift across successive valid snapshots.

OI interpretation must combine CE/PE option LTP changes, Greeks, spot context and OI change. OI change alone does not prove writing or buying. For example, rising OI with rising option LTP can indicate long build-up, while rising OI with falling option LTP can indicate short build-up; these remain hypotheses and must be checked across successive valid snapshots. Premium candles are not required.

## 8. Direction engine and explainability

Initial release should be a transparent deterministic evidence engine, not a model marketed as trained AI. It should evaluate:

- spot direction/context from the same snapshot and its successive observations;
- CE/PE option LTP changes across successive chain snapshots (not premium candles);
- Delta/Gamma/Theta/IV context;
- CE/PE OI and change-OI distribution;
- OI PCR and volume PCR (separately);
- Max Pain distance as context only;
- support/resistance distance and repeated reactions;
- liquidity: bid/ask spread, volume and quote availability;
- time of day and time remaining to expiry.

Output:
- `BULLISH`, `BEARISH`, `SIDEWAYS` or `WAIT`;
- confidence only if calibrated from out-of-sample data; otherwise use an evidence score, not a probability;
- positive evidence, negative/conflicting evidence and missing inputs;
- selected candidate contract or explicit no-trade reason;
- snapshot IDs and calculation/model versions.

Do not train XGBoost/LightGBM/LSTM or other models on a tiny or non-independent sample. A later ML phase requires chronological walk-forward validation, class balance reporting, calibration, transaction-cost/slippage assumptions and out-of-sample results. Keep ML output separate from deterministic rules until it proves incremental value.

## 9. Option-buying setup and signal lifecycle

A candidate must pass:
- directional alignment between spot context, selected option LTP movement, Greeks and OI/change-in-OI;
- sufficient liquidity and acceptable bid/ask spread where supplied;
- valid, fresh, internally consistent option-chain snapshots with an LTP for the exact selected contract;
- strike/expiry consistency;
- no conflicting high-severity signal;
- enough time for the 15-minute evaluation window;
- target and SL levels based on actual trigger/entry price.

Signal states:

```text
CANDIDATE -> ARMED -> TRIGGERED
                         |----> TARGET_HIT
                         |----> STOP_HIT
                         |----> TIME_EXPIRED
                         |----> INVALIDATED
                         |----> DATA_GAP (outcome unresolved)
```

- `CANDIDATE`: interesting contract, no alert.
- `ARMED`: setup conditions met; waiting for the defined premium trigger.
- `TRIGGERED`: entry trigger crossed by a successive validated option-chain LTP observation for the exact contract; persist trigger time and reference premium.
- `TARGET_HIT`: first valid post-entry option-chain LTP observation reaches entry + 20 points.
- `STOP_HIT`: first valid post-entry option-chain LTP observation reaches entry - 10 points.
- `TIME_EXPIRED`: neither target nor stop reached within 15 minutes.
- `INVALIDATED`: pre-defined invalidation occurs before trigger.
- `DATA_GAP`: missing candles prevent a reliable outcome; never force a win/loss label.

If a data gap spans both target and stop levels, or the observation interval is too wide to establish which threshold was reached first, label the outcome ambiguous/unresolved rather than choosing the favourable result. Include fees/slippage separately in net P&L calculations; the +20/-10 rule is gross premium points unless the user later changes it. The quality gate must also enforce a configurable maximum age between option-chain observations; delayed snapshots can make the exact touch order unknowable.

No automatic broker order placement.

## 10. Persistence model

Use SQLite with migrations. Recommended tables:

### `market_snapshots`
- id, provider, instrument, expiry, spot, source_timestamp, received_at, status, quality_json, raw_payload_json, schema_version.

### `option_chain_rows`
- id, snapshot_id, strike, expiry, CE fields JSON, PE fields JSON, unique(snapshot_id, expiry, strike).

### `candles`
- id, provider, instrument_type (SPOT or OPTION), symbol/contract key, interval, candle_timestamp, open, high, low, close, volume, source_timestamp, quality_status.
- Unique contract + interval + candle timestamp.

### `greeks_snapshots`
- id, snapshot_id, contract key, model, inputs_json, delta, gamma, theta, vega, rho, calculated_at.

### `analyses`
- id, snapshot_id, created_at, direction, evidence_score, confidence_calibrated, evidence_json, counter_evidence_json, missing_inputs_json, engine_version, decision (TRADE_CANDIDATE / WAIT / NO_TRADE).

### `signals`
- id, analysis_id, contract key, side, entry_trigger, actual_entry, target_price, stop_price, horizon_seconds, state, created_at, triggered_at, closed_at, outcome, gross_points, net_points, unresolved_reason.

### `signal_events`
- id, signal_id, event_type, event_timestamp, observed_price, snapshot_or_candle_id, details_json.
- Append-only audit trail for candidate, armed, triggered, target, stop, timeout and invalidation events.

Persist WAIT analyses too. A daily scheduled cleanup must not delete the audit trail. Index by timestamp, expiry, contract and signal state.

## 11. Dashboard information architecture

Retain the current clean, compact, responsive light/dark terminal. Add missing analytical and historical views; avoid presenting a large sidebar as the only navigation on mobile.

1. **Market Overview** — spot, expiry, feed quality, direction, key levels, Max Pain, PCR, last update.
2. **Option Chain** — full CE/PE matrix, strike filter, ATM highlight, bid/ask, volume, OI and change-OI.
3. **Greeks** — CE/PE Greeks by strike, IV, assumptions/provenance and ATM comparison.
4. **OI Intelligence** — OI and change-OI rankings, build-up/unwinding, PCR trends, shifting zones.
5. **Max Pain** — selected expiry, payout curve by settlement strike, max-pain history.
6. **AI Analysis** — direction, evidence breakdown, opposing evidence, selected candidate, WAIT reason.
7. **Option LTP & Alerts** — selected CE/PE live LTP trace from successive chain snapshots, trigger, entry, target, SL and 15-minute timer. No candle chart dependency.
8. **Analysis Journal** — all AI analyses, including WAIT/no-trade, searchable/filterable by date, direction, expiry and decision.
9. **Signal History** — full lifecycle and outcome events with replayable chart context.
10. **Performance** — target vs SL vs expired/unresolved; win rate on resolved trades only; CE/PE and time-of-day breakdown; average gross/net points; sample count and confidence interval.
11. **Export Centre** — Excel workbook with sheets: Summary, Analyses, Signals, Signal Events, Option Snapshots, OI Levels, Greeks, Data Quality.

Use honest empty states. Before data collection begins, performance should show "No recorded sample" rather than zero-percent accuracy. Pie chart should include target, stop, expired and unresolved categories; win rate denominator must be explicitly defined and exclude unresolved signals.

## 12. API outline

- `GET /api/health`: process health and provider status.
- `GET /api/market`: latest normalized snapshot and quality.
- `GET /api/chain?expiry=...`: selected-expiry chain.
- `GET /api/greeks?expiry=...`: Greeks and input provenance.
- `GET /api/oi?expiry=...`: PCR, rankings, zones and history.
- `GET /api/max-pain?expiry=...`: Max Pain and payout curve.
- `GET /api/analysis/latest`: latest direction and evidence.
- `GET /api/analyses`: paginated analysis journal.
- `GET /api/signals`: signal history and filters.
- `GET /api/performance`: resolved outcomes and counts.
- `GET /api/export.xlsx`: workbook export.
- Optional `GET /api/stream`: SSE updates from the local validated snapshot store; not a direct browser-to-exchange connection.

All endpoints should include snapshot IDs and timestamps. Paginate history and bound response sizes.

## 13. Test and release gates

### Unit tests
- Normalization preserves nulls and does not silently coerce missing values to zero.
- Freshness state transitions: LIVE / STALE / UNAVAILABLE.
- Duplicate strike/expiry rejection and mixed-snapshot rejection.
- Greeks numerical tests and unit labels.
- Max Pain against hand-calculated small examples.
- PCR and OI rankings including negative change-OI.
- Signal state transitions and exact +20/-10 boundaries using option-chain LTP observations.
- Target/SL ambiguity when observations are too far apart or a data gap occurs.
- 15-minute timeout and missing-snapshot handling.
- Excel sheets and row counts.

### Integration tests
- Approved provider contract and documented permission.
- Source timestamp advances during market hours.
- Spot and chain belong to same observation.
- Selected expiry and strike data are coherent.
- Data-gap and provider-outage recovery.
- Local Windows Python 3.12 startup.
- Render startup, persistent database strategy and restart recovery.

### Release criteria
Do not enable LIVE or alerts until an authorized provider passes market-hours verification. Do not show "AI accuracy" until a meaningful, chronologically out-of-sample signal sample exists. Do not deploy a data adapter that violates the source's terms.

## 14. Implementation sequence

- **Phase A — Foundation:** provider interface, normalized schema, data-quality gate, SQLite migrations and honest status handling.
- **Phase B — Analytics:** Greeks, OI/PCR, Max Pain, zones and unit tests.
- **Phase C — AI evidence:** explainable direction engine and multi-strike candidate ranking.
- **Phase D — Entry engine:** successive option-chain snapshot LTP trigger, 15-minute state machine and outcome journal. Premium candles are not required.
- **Phase E — Product UI:** Greeks, premium chart, analysis journal, signal history, performance and Excel workbook.
- **Phase F — Verification:** authorized live source, Windows 3.12 and Render tests.

**Current blocker:** Phase A's provider integration cannot be declared live until a permitted market-data source is identified and verified. Work on provider-independent calculations, storage, UI and tests can proceed without fabricating market data.
