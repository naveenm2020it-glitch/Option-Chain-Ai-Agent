# NIFTY Paper Trading v1

## Implemented
- Conservative option buy-at-ask / sell-at-bid accounting with adverse slippage.
- Quote freshness, timestamp, spread, contract identity and lot-size checks.
- Append-only PostgreSQL quote and paper-trade ledger.
- Authenticated FastAPI endpoints: `POST /api/paper/quotes`, `POST /api/paper/trades`, `GET /api/paper/performance`.
- One-shot provider adapter: `python quote_collector.py`. It expects a normalized JSON quote feed; it does not assume or scrape an undocumented exchange/broker endpoint.
- Every fee field is mandatory at trade simulation time. Supply rates from the actual broker tariff and record the tariff's effective date in your operational config.

## Required environment
- `DATABASE_URL`: persistent PostgreSQL; SQLite/local filesystem is not used for production ledger.
- `PAPER_INGEST_TOKEN`: long random secret shared only by collector and app.
- `PAPER_API_BASE_URL`: deployed app base URL.
- `QUOTE_PROVIDER_URL`: broker/vendor endpoint returning normalized quotes.
- `QUOTE_PROVIDER_TOKEN`: provider credential if required.

Do not deploy until PostgreSQL persistence, provider API permission, quote timestamps, lot sizes, and fee schedule are verified. Render Free filesystem is ephemeral; do not store the ledger there. Do not enable automatic polling until provider rate limits and exchange permissions are confirmed.

## Quote payload
`symbol, expiry, strike, option_type, bid, ask, timestamp, lot_size, source`.
Timestamp must be ISO-8601 with timezone. Quotes older than 15 seconds are rejected by default. Server-side freshness is configurable with PAPER_MAX_QUOTE_AGE_SECONDS (1–60 seconds); clients cannot relax the limit. Only NIFTY index option contracts are accepted.

## Trade payload
`entry_quote_id, exit_quote_id, lots, fees, slippage_bps`.
The `fees` object must contain brokerage_per_order, stt_sell_rate, exchange_turnover_rate, sebi_turnover_rate, stamp_buy_rate and gst_rate. All are decimal fractions except brokerage_per_order, which is a currency amount per order. Verify the precise tax bases and rounding with the broker tariff before relying on results.

## Not implemented / not authorized
No provider is selected or connected; no real quote has been collected. No strategy signal, automatic entry/exit, broker order API, or live trading exists. A 30–60 session forward paper trial is still required after a verified feed is connected.
