# Owned NIFTY MCP server

The application exposes its own MCP server over **Streamable HTTP** at:

- `https://YOUR-RENDER-DOMAIN/mcp`

It is mounted directly into the existing FastAPI website application, so the current Render start command remains compatible. The website continues to use its existing internal Python services; external MCP clients connect to the mounted MCP endpoint.

## Tools

| Tool | Purpose |
|---|---|
| `get_nifty_market_status` | Provider state, source timestamp and quality checks |
| `get_nifty_option_chain` | Verified LIVE selected-expiry chain |
| `analyze_nifty_option_chain` | Max Pain, PCR, OI and change-OI positioning candidates |
| `calculate_option_greeks` | Black-Scholes Greeks with explicit units |
| `get_nifty_signal_policy` | Target/SL/horizon and snapshot-monitoring policy |
| `get_recent_nifty_analyses` | Persisted research decisions |
| `get_nifty_signal_journal` | Signal lifecycle and outcomes |
| `get_nifty_performance` | Observed target/stop performance summary |
| `get_nifty_ai_analysis` | Current direction, candidate contract, Greeks/OI evidence and levels |
| `get_nifty_active_alerts` | Active alerts and monitoring status |

The MCP server is NIFTY-only and read-only. It cannot place orders or create fabricated signals. The signal-policy tool documents the requested +20 premium-point target, -10-point stop, 15-minute horizon, no premium-candle dependency and unresolved handling for ambiguous snapshot gaps. The actual signal lifecycle engine is a separate pending implementation and is not represented as complete by this MCP server.

## Render configuration

Set these environment variables on the existing Render web service:

- `MCP_ACCESS_TOKEN`: long random secret (required to enable external MCP access).
- `NIFTY_DATABASE_PATH`: optional SQLite path; default `data/nifty_research.sqlite3`.

The MCP endpoint returns HTTP 503 while `MCP_ACCESS_TOKEN` is absent and HTTP 401 for invalid bearer tokens. Clients must send:

```http
Authorization: Bearer YOUR_MCP_ACCESS_TOKEN
```

MCP URL: `https://YOUR-RENDER-DOMAIN/mcp`

Do not commit the token to GitHub or expose it in browser JavaScript. Configure it only in Render's environment settings. For a browser-based ChatGPT connector, a future OAuth authorization flow should replace the shared bearer token; never paste the token into a public prompt or client-side website code.

## Local run

```powershell
py -3.12 -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
python -m pip install -r requirements.txt
$env:MCP_ACCESS_TOKEN = "replace-with-a-long-random-secret"
uvicorn app:app --host 127.0.0.1 --port 8000
```

Website: `http://127.0.0.1:8000/`

MCP endpoint: `http://127.0.0.1:8000/mcp`

## Data entitlement

MCP is a protocol, not a market-data entitlement. The MCP server passes through the same website provider and its data-quality gate. If the provider is disabled, unapproved, stale, incomplete or unavailable, market tools return WAIT/UNAVAILABLE with no chain rows. Enabling external MCP access does not enable NSE collection or make an unofficial endpoint permitted.

## Signal engine and website APIs

The website now exposes `/api/signals`, `/api/signals/{id}/events`, `/api/history`, `/api/performance`, and `/api/export/signals.xlsx`. The signal engine compares distinct verified option-chain snapshots, derives directional confluence from spot, OI/change-OI and selected-contract LTP, estimates Greeks when IV and expiry inputs are valid, and creates a candidate only after its rule threshold is met. Entry uses the selected contract's ask quote; target is entry +20 premium points and stop is entry -10 points. It monitors only subsequent LTP snapshots for up to 900 seconds. Gaps over 15 seconds and ambiguous jumps are recorded as UNRESOLVED rather than guessed. No premium candles or broker orders are used. This is a transparent heuristic engine, not a trained model or validated probability forecast.

SQLite is suitable for a single-instance research deployment only. Render free instances may lose local data on restart; configure persistent storage or managed Postgres for reliable long-term journaling.

The server is mounted at `/mcp` on the same Render website and protected by a bearer token (`MCP_ACCESS_TOKEN`). Connect an MCP-compatible client using Streamable HTTP, the website's `/mcp` URL and the configured bearer token. The website's own MCP exposes read-only market/analysis/journal tools; it cannot place broker orders.
