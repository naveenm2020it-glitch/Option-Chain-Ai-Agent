# Phase 3 historical checkpoint validation

## Result

- Source: previously audited historical context checkpoint, envelope schema version 2.
- Per-session feature schema: version 3.
- Date range: 2024-11-12 through 2026-11-12.
- Sessions inspected: 494.
- Sessions normalized successfully: 494.
- Sessions rejected: 0.
- Normalized NIFTY CE/PE contract rows: 159,036.
- Contracts per session: minimum 260; maximum 384; mean 321.94.
- Deterministic session dataset fingerprints generated: 494; unique fingerprints: 494.
- First session fingerprint: `3610b4f56e0ef2cb0750f879f1c8870a3bfd8fdf8bb3ed37acd05b060f54d882`.
- Last session fingerprint: `8aef1d540876033a574876ad7d0b0c53b46d3c7bba44223918148fffbf197dd3`.

## Validation performed

- Confirmed checkpoint envelope and feature schema versions.
- Validated session key alignment.
- Validated non-empty feature maps and contract lists.
- Validated expiry dates are not before session dates and days-to-expiry arithmetic.
- Validated CE/PE option types and signed delta conventions.
- Validated finite numeric fields, positive strike/close, and nonnegative previous close, OI, and volume.
- Checked duplicate session/expiry/strike/option-type contract keys.
- Validated archive SHA-256 provenance format where present.
- Generated canonical sorted JSON fingerprints using the Phase 3 normalizer's deterministic serialization rules.

## Important boundary

This was a read-only validation of the historical checkpoint content. It did not connect to PostgreSQL, create tables, or write rows. Synthetic CI tests passed, but no production database persistence or database integration test has been performed.

**Status:** historical input normalization acceptance passed. PostgreSQL persistence acceptance remains pending an explicit database-write approval and a successful database integration run.
