# NIFTY AI Intelligence Expansion — Audit and Phased Implementation Plan

Audit date: 2026-11-11
Repository: `naveenm2020it-glitch/Option-Chain-Ai-Agent`
Scope: NIFTY 50; the 12 configured NSE end-of-day (EOD) report types only.
Explicit exclusions: intraday candle feeds, broker APIs, paid APIs, fabricated values, and unvalidated probabilities.

## 1. Repository baseline

The current `main` head at audit time is `dae54a8ec4df412ca3bdf7095519ad1adbb42902`. The latest GitHub CI run (run 43008023427) completed successfully. The latest Render deployment for that commit (deploy `dep-dbq4khi6ffts73a2ipl0`) is marked `live`.

Existing foundations found:
- Twelve report types are registered in `nifty_ai/reports.py`.
- Three NSE collectors are run by `.github/workflows/nse-eod-report-collector.yml`; the workflow runs the test suite 30 times before collection.
- The analysis already extracts NIFTY OHLC, India VIX, option OI/change/volume/premium, FII/DII activity, participant summaries, delivery, breadth, volatility and settlement report metadata.
- `nifty_ai/eod_intelligence.py` already contains nearest-expiry option parsing, call/put writing proxies, OI migration comparison, institutional positioning, daily-candle absorption proxy, next-day regime classification, zone confluence and retrospective zone-touch validation.
- The UI already displays agents, AI support/resistance, buyer/seller defence zones, and retrospective zone validation.

### Archive coverage observed in manifest

The manifest records 38 available sessions in each of 11 report categories. FII/DII cash activity has only one available session (2026-11-09). Other categories each record five unavailable dates in the scanned period.

This is an archive count, not proof that every report has the required columns for every date. Each analysis run must validate the actual date, schema, row count and required fields.

## 2. Audit findings and risks to resolve

1. **Agent count/documentation mismatch.** Current augmentation builds 12 specialists plus Final Consensus (13 total), and consensus completeness uses 12 specialists. The page/settings still contain references to 10 specialists + final consensus / 11 agents. The methodology text in the backend also incorrectly describes ten specialists and says Agent 8 is consensus. Reconcile names, counts and copy from a single registry.
2. **FII/DII history is insufficient.** One available FII/DII session cannot support multi-session accumulation/distribution or robust FII/DII trend claims. Show one-day flow only and mark trend/migration analysis insufficient until enough dated history exists.
3. **Participant volumes are not positions.** Keep traded volume separate from open interest and end-of-day participant positions. Do not infer long/short holdings from volume-only data.
4. **FII derivatives traded flow is not open interest.** Preserve the distinction between buy/sell contract flow and end-of-day OI.
5. **Writing/covering classifications are proxies.** OI changes plus premium movement can support writing/covering labels, but do not prove intent. Require previous-session comparability, volume/liquidity checks and explicit proxy labels.
6. **Current zone validation is retrospective, not full forecast validation.** Existing walk-forward zone-touch checks and Wilson intervals do not replace timestamped next-day forecasts matched against later outcomes. Probability calibration remains disabled until a prospective prediction/outcome ledger is implemented.
7. **Daily-only limits.** No intraday candles means no 5m/15m/hourly analysis, no intraday order-flow absorption, and no intraday fake-breakout detection. Keep these modules visibly LIMITED — EOD DATA ONLY. Daily closing break/retest analysis may be offered with appropriately narrow labels.
8. **Daily range / gap scenarios need careful framing.** Expected range can be estimated from historical daily ranges and VIX-conditioned distributions. Gap scenarios can be modelled from historical daily open-versus-prior-close data, but the next session's actual gap is unknown before its open.
9. **Schema validation needs to be feature-specific.** A file being parseable or present in the manifest is not enough. Record per-feature availability, missing columns, date alignment and source provenance.
10. **Render storage is ephemeral on the free filesystem.** Long-term history and prediction outcomes need durable backup/persistence outside the ephemeral service filesystem; do not assume redeploys preserve SQLite/uploads.

## 3. Scope and status policy for the 22 requested modules

### Institutional
1. FII/DII accumulation and distribution — supported by FII/DII cash report plus delivery/breadth context; historical trend is gated by sample coverage.
2. FII futures long/short positioning — participant OI and FII derivatives; separate end-of-day OI from traded flow.
3. Participant-wise options positioning — participant OI; participant volume is contextual activity only.
4. Institutional position migration — compare consecutive dated participant OI and FII derivatives snapshots on consistent categories/instruments.
5. Institutional hedging detection — identify opposing futures/options exposures as a hedge candidate, not proof of intent.

### Derivatives
6. Strike-wise CE/PE OI pressure — F&O bhavcopy by NIFTY strike and expiry.
7. Call/put writing detection — OI increase plus premium decrease proxy, with price/volume/expiry context.
8. Short covering/long unwinding — OI decrease combined with premium movement; proxy classification only.
9. Expiry/settlement pressure — F&O bhavcopy and settlement prices, aligned by contract and expiry.
10. Option volatility — F&O volatility, option prices and India VIX; Greeks only if all required inputs are available and estimates are clearly labelled.

### Price action and scenarios
11. Demand/supply discovery — daily swing structures, rejection history and option evidence.
12. Multi-timeframe support/resistance — daily and longer EOD-derived horizons only. Intraday timeframes are LIMITED/unavailable.
13. Breakout/breakdown probability — daily-close breakout definitions and historically calibrated outcomes only.
14. Fake breakout detection — intraday version LIMITED. EOD-only failed-break/re-entry patterns may be tested as a separate daily pattern.
15. Next-day expected range — daily ATR/range distributions and VIX-conditioned historical ranges; report interval and sample count.
16. Gap-up/gap-down scenarios — historical daily open gaps and conditional scenario frequencies; no prediction of a guaranteed opening gap.

### Predictive and validation
17. Next-day market regime — daily trend/range/volatility classification.
18. Historical pattern matching — compare EOD candle, volatility and positioning feature vectors using only prior observations.
19. Zone holding probability calibration — prospective zone forecasts matched to future daily OHLC outcomes; no probability before adequate samples/calibration.
20. FII + options + price confluence — combine independent source groups with duplicate-evidence controls.
21. Walk-forward accuracy — chronological expanding/rolling training and strictly later test windows, with no look-ahead leakage.
22. Agent disagreement/uncertainty — report conflicting direction, missing evidence, low sample size and unstable inputs; disagreement must reduce readiness.

## 4. Phased implementation

### Phase 0 — Audit and integrity baseline
- Preserve current formulas and separate traditional reference levels from AI-discovered zones.
- Establish a canonical agent registry and reconcile 12 specialists + consensus naming/count.
- Build a report/feature availability matrix by trading date and schema.
- Add tests for missing dates, duplicate files, incomplete columns, mismatched dates and stale data.
- Gate: all existing tests pass; no data-quality regressions; dashboard agent count equals backend registry.

### Phase 1 — Institutional intelligence
- Normalize FII/DII, participant OI, participant volume and FII derivatives into typed daily records.
- Implement cash accumulation/distribution, futures net OI, participant option exposure, migration and hedge-candidate analysis.
- Enforce separation of cash flows, traded volume, traded contracts and open interest.
- Gate: fixture tests against hand-calculated examples; multi-date alignment tests; one-day FII/DII data cannot emit a multi-session trend.

### Phase 2 — Derivatives intelligence
- Normalize NIFTY CE/PE strike/expiry OI, OI change, volume, close and previous close.
- Add strike pressure, writing/covering proxies, expiry/settlement context and volatility analysis.
- Gate: expiry isolation, contract filtering, sign/unit checks, premium/OI combination tests and missing-field tests.

### Phase 3 — EOD price action and scenario intelligence
- Add daily demand/supply zones, daily/weekly/monthly levels, daily-close breakout/breakdown outcomes, daily pattern failure and historical gap scenarios.
- Add daily expected-range distributions with VIX context.
- Explicitly mark intraday multi-timeframe and intraday fake-breakout functions LIMITED.
- Gate: chronological candle fixtures, no pivot substitution, correct next-session target alignment and no intraday labels.

### Phase 4 — Predictive confluence and uncertainty
- Build historical feature snapshots and pattern matching.
- Add confluence with source-group de-duplication, disagreement and missing-data penalties.
- Keep raw evidence scores separate from calibrated probabilities.
- Gate: feature provenance, no leakage tests, agent disagreement fixtures and probability suppression for low sample sizes.

### Phase 5 — Prospective validation and dashboard activation
- Persist each forecast with its generation timestamp, source dates, feature snapshot, zones, scenario and model version before the outcome is known.
- Match next-session daily OHLC and evaluate direction, range, zone holds/failures and breakout outcomes.
- Run expanding-window walk-forward evaluation and calibration diagnostics.
- Activate probabilities only after predeclared sample-size, calibration and stability criteria pass.
- Gate: reproducible backtest report, baseline comparison, confidence intervals, false-signal rates and regression suite pass.

## 5. Required UI statuses

Use explicit statuses rather than hiding unavailable modules:
- READY — required source fields and history are present.
- PARTIAL — some evidence is available, but one or more inputs are missing.
- INSUFFICIENT HISTORY — not enough dated observations.
- LIMITED — EOD DATA ONLY — requested intraday capability cannot be derived.
- NOT CALIBRATED — a numeric probability is not yet validated.
- DISABLED — a feature is withheld because its input or validation gate failed.

Every module should expose source report(s), source trading date(s), fields used, method, sample count, limitations and status.

## 6. Acceptance criteria

- No paid/broker/intraday data sources are introduced.
- No fabricated or stale values are substituted.
- Existing NIFTY level calculations and separation of AI zones from traditional levels remain intact.
- All 22 modules are represented in a traceable registry with source requirements and status.
- Each phase has unit, integration, missing-data and chronological leakage tests.
- The dashboard never presents evidence scores as probabilities.
- No probability is activated before prospective outcome logging and calibration.
- UI agent/module counts are generated from backend registry metadata, not hardcoded text.
