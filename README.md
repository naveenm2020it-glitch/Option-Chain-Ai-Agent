# NIFTY Option Chain Intelligence

A responsive NIFTY 50 option-chain research terminal. Live market-data integration is currently disabled until a permitted provider is configured and verified.

## Current features
- Honest UNAVAILABLE status while no permitted live provider is configured.
- NIFTY spot, ATM and selected expiry.
- Full selected-expiry CE/PE option chain.
- OI concentration support and resistance levels, PCR and change in OI.
- Max Pain calculation from current open interest and intrinsic payout.
- Rules-based multi-strike watchlist using premium change, OI change, volume, bid/ask spread and distance from ATM.
- Light and dark themes, mobile-responsive navigation and CSV exports.

## Data integrity and limitations
No permitted free, no-key, non-broker automated NIFTY option-chain source has been verified. The previous NSE website endpoint poller has been disabled because the endpoint is undocumented and the source terms do not establish permission for continuous automated collection. The app therefore returns UNAVAILABLE and pauses analysis/alerts until an authorized provider is configured. Do not interpret a successful HTTP response from an undocumented website endpoint as permission or as proof of a reliable live feed.

The strike ranking is a transparent rules-based screening heuristic, not a trained or validated AI model. It is not an entry recommendation. No live provider or synchronized spot/option-premium candle feed is currently connected. Candle-confirmed entry triggers, target/stop trigger timestamps and win-rate statistics are deliberately disabled and not fabricated.

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

## Run locally (Python 3.12)
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app:app --reload
```
Open http://127.0.0.1:8000

## Render
The repository includes a Render Blueprint. The current research branch has not been deployed or tested on Render. Free Render instances may spin down; configure persistent storage before relying on SQLite history in production.

Research tool only. Not investment advice.
