# Phases 01–03 — implementation, evidence and acceptance record

**Last reviewed:** 2026-11-20  
**Repository branch reviewed:** `feature/paper-trading-engine-v1`  
**Project mode:** historical/EOD research and paper trading only. No live orders or automatic execution.

This document separates code implementation, test evidence, operational execution and phase acceptance. A merged pull request is not, by itself, proof that a production data pipeline has run successfully.

## Executive status

| Phase | Implemented work | Evidence available | Remaining acceptance gate |
|---|---|---|---|
| 01 — Historical data audit | Archive integrity audit, calendar reconciliation, all-strike CE/PE feature extraction, resumable checkpoints and coverage reporting | Older artifact: 494/494 sessions, zero failed archives and zero feature errors | Re-run audit after contract-universe v1/all-strike changes; require 494/494 current-version feature rows and at least 95% all-strike CE/PE coverage |
| 02 — User configuration and data orchestration | Historical datasets plus date selector, EOD-only cutoff guard, archive-derived expiry choices and signed archive download handoff | PR #27 merged; PR #38 adds the Phase 2 API/UI and regression tests; full CI and Phase 3 validation passed on the current branch head | Verify against real B2 archives and complete downstream ingestion/analysis integration; intraday cutoffs remain unsupported |
| 03 — Storage and data processing | All-strike contract normalization, PostgreSQL schema, atomic/idempotent persistence, source fingerprints, remote authenticated ingestion API and integrity verifier | PRs #28–#33 and #36–#37 merged to deployed branch; 494-session/159,036-row historical normalization report from the earlier checkpoint | Rebuild/verify all-strike checkpoint, initialize actual Render schema, ingest all sessions, and verify stored session/contract counts and fingerprints against source |

## Phase 01 — Historical data audit

### Work implemented

- `scripts/nse_historical_audit.py`: resumable, bounded audit of the 2024-11-12 through 2026-11-12 historical window.
- `scripts/nse_historical_backfill.py`: remote archive validation using archive SHA-256, ZIP CRC, embedded manifest checks, per-report hashes and payload validation.
- `scripts/nse_ml_features.py`: NIFTY futures and options feature extraction, including CE and PE contract snapshots.
- NSE trading-calendar reconciliation, including the explicitly declared special Muhurat session.
- Checkpoint and coverage reports with missing, pending, failed, feature-error and data-availability fields.
- The audit is read-only against B2; it does not silently backfill missing archives.

### Evidence already inspected

The GitHub Actions artifact `nifty-historical-audit-43691154808` contains a coverage report with:

- 494 expected sessions and 494 verified sessions.
- Zero failed sessions and zero feature extraction errors.
- No missing or pending archive dates.
- Core NIFTY options and futures availability reported at 100%.
- Optional index and participant context availability was incomplete in that older artifact; optional sources were explicitly availability-flagged rather than fabricated.

### Why that older PASS is not the final current PASS

The 2026-11-20 implementation introduced `contract_universe_version=1` and all-strike coverage gates. The older 2026-11-15 artifact predates that change. It cannot prove the current all-strike checkpoint has been rebuilt and validated.

### New automation in this branch

`scripts/run_full_historical_audit.py` resumes the existing checkpoint in bounded batches of at most 25 archives, stops safely on a B2 request-cap response, and returns success only when all 494 sessions, feature extraction, contract-universe version and all-strike coverage gates pass. It does not write to B2 or fetch new NSE data.

`.github/workflows/phase1-3-acceptance-audit.yml` runs this bounded audit on the dedicated acceptance branch or by manual dispatch and publishes the checkpoint and coverage artifact. The original `nifty-historical-integrity-audit.yml` remains manual-only and unchanged in its operational trigger policy.

**Phase 01 status: implementation substantially complete; current all-strike audit rerun and evidence publication pending.**

## Phase 02 — User configuration and data orchestration

### Work implemented

- PR #27, merged as `5b62220a221043c8baf1b33158e261b6e4cdb832`, added the schema-v3 historical dataset preparation pipeline.
- The reported dataset build used 494 source sessions to create 472 samples after a 22-session warm-up.
- Each sample has 182 features, with 5- and 22-session rolling comparisons.
- Four chronological research datasets were prepared: direction, range, CE seller premium and PE seller premium.
- The reported split was 330 train / 71 validation / 71 holdout.
- CE and PE seller target coverage was reported as 472/472.
- Regression tests, historical dataset build and structural artifact audit were reported passing.
- The NSE EOD collector/backfill tooling and session-alignment quality gates are present in the repository.

### Data-quality caveats that must remain visible

- Seller labels use a provisional +/-1% threshold and are not economically calibrated.
- The PR #27 audit marked seller-model training readiness as `REVIEW_REQUIRED`; expiry-day targets and transaction-cost-aware label definitions require review.
- The samples are explicitly after-close. EOD archives cannot create a genuine intraday cutoff snapshot.
- Participant positioning is broad NSE index-derivatives context, not proof of NIFTY-specific seller identity.

### Remaining roadmap work

PR #38 now adds a dashboard configuration panel and `GET /api/config/options`. The endpoint validates the requested date against the B2 archive catalog, reads that session's archive, derives the actual unexpired NIFTY CE/PE expiry list from the contract universe, defaults to the front expiry, validates any selected expiry, and returns a 15-minute signed download URL. The browser retains the selected date/cutoff/expiry locally and caches the options response briefly to avoid repeated B2 reads.

**Required cutoff behavior:** until intraday source data is approved and available, expose only an after-close/EOD snapshot. Reject or clearly mark intraday cutoff requests as unsupported; never label EOD data as an intraday snapshot.

**Phase 02 status: configuration UI/API and archive download handoff are implemented and regression-tested in PR #38; live B2 integration and downstream ingestion/analysis wiring remain acceptance items.**

## Phase 03 — Storage and data processing

### Work implemented

- PR #28 added schema-v3 NIFTY CE/PE contract normalization, PostgreSQL relational schema, constraints, provenance and deterministic SHA-256 fingerprints.
- The historical validation report for the earlier checkpoint reported 494/494 sessions normalized, 159,036 contract rows, 494 unique fingerprints and zero rejected sessions.
- PR #29 added post-write verification of session metadata, actual contract row counts and dataset fingerprints.
- PR #31 integrated the processor into the Render-deployed branch.
- PR #32 added a read-only database health endpoint.
- PR #33 added authenticated schema initialization; it creates tables/indexes only and does not ingest rows.
- The all-strike universe was versioned separately from feature schema. Far OTM strikes and expiries beyond 45 days are retained.
- PR #36 and PR #37 added regressions for stored contract counts and the ten-session remote ingestion batch guard.
- `scripts/ingest_phase3_remote.py` validates the all-strike coverage artifact before sending bounded batches to the protected Render API.
- `scripts/process_nifty_contracts.py` remains read-only by default; database writes require explicit `--write-db`.
- `.github/workflows/phase3-remote-historical-ingestion.yml` now provides a manual, authenticated ingestion path from a successful all-strike audit artifact. It requires the existing Render `PAPER_INGEST_TOKEN` to be stored as a GitHub Actions repository secret; it never stores the token in source code.

### Production boundary

Code and synthetic tests do not prove production PostgreSQL persistence. The live Render database check attempted during this review was blocked because the database's external IP allowlist is empty. The intended design is private Render-to-Render connectivity using the Internal Database URL, not opening PostgreSQL to the public internet.

The Render database resource reviewed is on the Free plan and reports an expiry date of 2026-12-10. Durable use beyond that date requires a deliberate storage/retention decision; do not silently migrate or incur a paid plan.

### Remaining operational acceptance

1. Ensure the deployed Render service uses the PostgreSQL Internal Database URL as `DATABASE_URL`.
2. Confirm `/api/phase3/health` reports the database connected and both Phase 3 tables present.
3. Run the current all-strike audit and obtain its current checkpoint and coverage artifact.
4. Store the existing Render `PAPER_INGEST_TOKEN` in GitHub Actions secrets under the same name; do not paste tokens into GitHub files, logs or chat.
5. Run `.github/workflows/phase3-remote-historical-ingestion.yml` with the successful audit run ID.
6. Ingest in batches of no more than ten sessions.
7. Verify all 494 session records, the total contract rows, per-session contract counts and SHA-256 fingerprints.
8. Confirm read-back from PostgreSQL and preserve the verification report.

**Phase 03 status: normalization, schema and ingestion code are implemented; production schema/data persistence is not yet verified.**

## Safety and scope boundaries

- Keep NIFTY as the primary option universe and preserve all eligible CE and PE strikes.
- Do not use ATM-only filtering as a substitute for the all-strike universe.
- Do not infer intraday snapshots from EOD data.
- Do not invent missing reports, features, expiries, deltas or contract rows.
- Do not expose B2, PostgreSQL or paper-ingestion credentials.
- Do not open PostgreSQL to `0.0.0.0/0`.
- No live broker feed, live order placement, automatic trade execution or model training is part of these three phases.
- The Render paper-trading database expiry must be addressed before production durability is claimed.

## Acceptance checklist

- [ ] Phase 01 current-version all-strike audit: 494/494 verified; zero failures/errors; at least 95% all-strike CE/PE coverage.
- [ ] Phase 02 date, EOD cutoff, automatic expiry discovery and signed historical download path pass live B2 end-to-end validation; selected settings are consumed by the downstream analysis pipeline.
- [ ] Phase 03 Render schema initialized and both tables verified.
- [ ] Phase 03 all-strike historical ingestion completed and source-to-database counts/fingerprints match.
- [ ] User reviews the evidence and accepts each phase.

**Current decision:** Phases 01–03 have substantial merged implementation, but none should be marked fully accepted until the unchecked operational/configuration gates are satisfied.
