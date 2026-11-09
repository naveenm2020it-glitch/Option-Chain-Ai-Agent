# NIFTY Option Chain Intelligence

A responsive NIFTY 50 option-chain research terminal. Live market-data integration is currently disabled until a permitted provider is configured and verified.

## Current features
- Honest UNAVAILABLE status while no permitted live provider is configured.
- NIFTY spot, ATM and selected expiry.
- Full selected-expiry CE/PE option chain.
- Per-strike CE/PE Black–Scholes Greeks matrix (Delta, Gamma, Theta, Vega and Rho) from provider IV when available.
- OI concentration support and resistance levels, PCR and change in OI.
- Max Pain calculation from current open interest and intrinsic payout.
- Snapshot-confluence signal engine using successive verified option-chain snapshots, spot movement, contract LTP, OI/change-OI, liquidity and model Greeks.
- Signal lifecycle with entry timestamp, +20 premium-point target, -10 point stop, 15-minute horizon and conservative UNRESOLVED handling when snapshot gaps obscure event order.
- Persistent SQLite signal journal, event history, outcome summary and multi-sheet Excel export.
- Light and dark themes, mobile-responsive navigation and CSV exports.

## Data integrity and limitations
No permitted free, no-key, non-broker automated NIFTY option-chain source has been verified. The previous NSE website endpoint poller has been disabled because the endpoint is undocumented and the source terms do not establish permission for continuous automated collection. The app therefore returns UNAVAILABLE and pauses new entries until an authorized provider is configured. Do not interpret a successful HTTP response from an undocumented website endpoint as permission or as proof of a reliable live feed.

The strike ranking is a transparent rules-based screening heuristic, not a trained or validated AI model. It is not an entry recommendation. No live provider is currently approved or connected. The snapshot-based entry lifecycle is implemented but requires a genuinely permitted, fresh provider before it can issue entries. It does not use premium candles. The confluence rules are transparent heuristics, not a trained or validated AI model. Entry alerts also require the latest observed option-premium velocity to extrapolate to the entry ask +20-point target within 15 minutes. This is a simple two-snapshot feasibility screen, not a calibrated probability forecast or guarantee; 15 minutes is the maximum monitoring horizon.

Max Pain is calculated by testing each available strike as a settlement price and summing estimated intrinsic payouts weighted by current CE/PE open interest. It is a positioning reference, not a forecast.

## Optional MCP provider integration

A read-only stdio MCP adapter is implemented in `providers/mcp_adapter.py`. It discovers the configured server's tools, calls only the explicitly allowlisted tool, and expects a JSON normalized snapshot. The adapter is disabled by default.

1. Review the exact MCP server repository, upstream data source, license and terms for automated collection, storage and display.
2. Confirm that the selected read-only tool returns a normalized NIFTY snapshot including a genuine timezone-aware `source_timestamp`, spot, expiry list, selected expiry and CE/PE rows.
3. Copy `.env.example` into your local environment configuration and set the server command, arguments, exact tool name and tool arguments.
4. Only after the upstream source terms have been reviewed and automated use is permitted, explicitly set `MCP_PROVIDER_ENABLED=true` and `MCP_AUTOMATED_USE_PERMITTED=true`.
5. Adjust `MCP_MAX_AGE_SECONDS` only to a justified source delay. A receive timestamp is never substituted for a missing source timestamp.

For Render, configure the MCP server command and arguments only if the server can be installed and launched in the same service/container. The adapter currently implements local stdio transport; hosted HTTP MCP transports are not implemented. Do not enable an unofficial NSE endpoint wrapper for production without the required permission. A successful tool response alone never means LIVE; the existing quality gate must pass.

The MCP adapter does not include any NSE scraper, bypass, CAPTCHA workaround or automatic order placement. It does not make an unapproved source compliant. When disabled, unapproved, stale or malformed, the app remains UNAVAILABLE/STALE and alerts remain paused.

## Production backend update architecture

The dashboard now uses a backend WebSocket at `/ws/market` for push notifications. The backend sends a 3-second connection heartbeat; this heartbeat is **not** a market tick and never changes market values. Provider status and source-timestamp changes are pushed separately. The browser uses a low-frequency REST fallback only when the WebSocket is disconnected.

The 3-second setting is an analytics/UI responsiveness target, not a promise that NSE publishes new data every three seconds. Analytics and signal lifecycle processing run only when a new, unique, fresh, permitted source snapshot arrives. Provider polling/cache cadence is separately configured with `PROVIDER_POLL_INTERVAL_SECONDS` and `PROVIDER_CACHE_TTL_SECONDS`; do not set these to three seconds merely to make the screen look live.

The Render deployment profile is fail-closed: `NSE_AUTOMATED_COLLECTION_ENABLED=false`, `MCP_PROVIDER_ENABLED=false`, `JUGAAD_PROVIDER_ENABLED=false`, `FYERS_PROVIDER_ENABLED=false`, and `ORDER_EXECUTION_ENABLED=false`. NSE data can be imported manually from a user-downloaded CSV for research, but imported snapshots are never labelled LIVE and cannot trigger entry signals. No NSE website JSON polling, browser scraping, CAPTCHA/session bypass, or automated collection is implemented.

The live-provider interface remains provider-neutral. A future provider must have documented permission for automated collection and display, a genuine exchange/source timestamp, complete CE/PE rows for one expiry, and must pass `services/data_quality.py` before the app can report LIVE. A WebSocket connection or HTTP 200 alone does not establish data permission or freshness.

## Run locally (Python 3.12)
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app:app --reload
```
Open http://127.0.0.1:8000

## Render
The `main` branch is connected to the Render web service and the current application build is deployed. The Python 3.12 CI suite is used for regression checks. The service uses a provider polling loop only for an explicitly enabled and permitted provider. The dashboard WebSocket sends a 3-second heartbeat and market-update notifications when a new provider snapshot arrives. The free Render web service can spin down, and its local SQLite filesystem is not durable across all restarts/redeploys; use a persistent database before relying on long-term signal history. Render's free web service can spin down, and its local SQLite filesystem is not durable across all restarts/redeploys; use a persistent database before relying on long-term signal history.

Research tool only. Not investment advice.


## Owned MCP server

The application now exposes an authenticated, read-only Streamable HTTP MCP server at `/mcp`. See [docs/OWN_NIFTY_MCP_SERVER.md](docs/OWN_NIFTY_MCP_SERVER.md) for available tools, Render setup and client authentication. Configure `MCP_ACCESS_TOKEN` only in Render environment settings; never commit the token. The MCP server uses the same provider permission and freshness checks as the website. The Render service has a bearer token configured; the `/mcp` endpoint rejects requests without the matching Authorization header. The upstream market-data provider remains disabled until a permitted source is configured.


## FYERS read-only market data (optional)

A documented FYERS provider is implemented in `providers/fyers_provider.py`. It imports only the FYERS market-data REST client and Data Socket; it does not import or call any order-placement, modification, cancellation or trading methods.

### Standard-plan design and limitations

- Initial/periodic REST Option Chain request: NIFTY index, 24 strikes on either side of ATM, with Greeks enabled.
- Data Socket: NIFTY index plus 49 CE and 49 PE contracts (99 symbols total), staying within the documented 100-symbol Standard-plan cap.
- WebSocket updates: LTP, daily OHLC, change/percentage change, traded volume, bid/ask prices and sizes. Index updates provide LTP and daily OHLC/change fields.
- REST snapshot fields: OI, change in OI, previous OI, volume, bid/ask, IV, and Delta/Gamma/Theta/Vega. OI/change-OI/IV/Greeks are **not** included in the documented SymbolUpdate WebSocket payload; they refresh on the configured REST interval (default 60 seconds).
- At a 60-second REST refresh, the app makes about 375 option-chain requests in a 6h15m session, rather than 7,500 requests from polling every 3 seconds. Actual limits are governed by FYERS' current Standard plan (5 requests/second, 50/minute, 5,000/day for Data APIs).
- This is a selected 49-strike window, not the entire listed expiry universe. Standard WebSocket subscriptions are capped at 100 symbols per user across all data-socket connections.

### Configure on Render

1. Create a FYERS **Individual Non-Trading App** (data-only) in the FYERS API Dashboard. It provides market-data access without order-placement permission. Obtain its App ID and access token through FYERS' official authentication flow.
2. In Render → service → Environment, set `FYERS_PROVIDER_ENABLED=true`, `FYERS_APP_ID` and `FYERS_ACCESS_TOKEN`. Keep the token server-side only; never put it in HTML, JavaScript, browser storage, GitHub, or a public MCP response.
3. Keep `FYERS_REST_REFRESH_SECONDS=60` initially. The provider will subscribe to 99 symbols at most on Standard.
4. Redeploy. Check `/api/providers/fyers` for configuration/stream status and `/api/health` for service status.
5. FYERS access tokens expire daily at 6:30 AM IST; generate a fresh token after expiry and update the Render environment value. The first valid LIVE state requires a timestamped exchange WebSocket update. No live feed can be claimed until a real authenticated connection is observed.

### Important

FYERS API access requires a FYERS account and valid authentication. FYERS documentation describes the Standard plan as the default and states free market-data access; the API rate limits and socket symbol limits are plan-specific and may change. This integration does not guarantee 24/7 uptime, does not bypass authentication, and does not place orders. The current dashboard's existing signal engine remains data-quality gated.
