# NIFTY LAB · Option Chain Intelligence

A responsive NIFTY 50 option-chain research terminal.

## Live market-data provider

The backend supports the **Upstox Analytics Token** read-only market-data API for NIFTY option-chain snapshots. Upstox documents this token as free, valid for one year, read-only, and usable for market quotes, option chains and WebSocket market data. It does not enable order placement.

### Configure on Render

1. Create or sign in to an Upstox account.
2. Open the Upstox Developer Apps page and generate an **Analytics Token** (not an order/trading token).
3. In Render, open the NIFTY LAB web service → Environment.
4. Add `UPSTOX_ACCESS_TOKEN` and paste the token as its value.
5. Save changes and redeploy/restart the service.
6. Open `/api/health`, then `/api/market` and `/api/analysis` to verify the provider response.

**Never commit the token to GitHub or put it in frontend JavaScript.** Store it only as a Render secret environment variable. Do not paste it into chat.

The backend calls:
- `GET https://api.upstox.com/v2/option/contract` to obtain available NIFTY expiries.
- `GET https://api.upstox.com/v2/option/chain` to retrieve the selected expiry's CE/PE chain.

The adapter normalises the provider JSON into the existing NIFTY LAB response structure. It maps OI, previous OI, volume, LTP, bid/ask, IV and provider Greeks. Change in OI and premium change are derived from the provider's current and previous values. The response-received time is shown because this option-chain endpoint does not expose a source tick timestamp in its documented schema.

## Current features

- Live / STALE / UNAVAILABLE source-state handling.
- NIFTY spot, ATM and selected expiry.
- Full selected-expiry CE/PE option chain.
- OI concentration support and resistance levels, PCR and change in OI.
- Max Pain calculation from current open interest and intrinsic payout.
- Rules-based multi-strike watchlist using premium change, OI change, volume, bid/ask spread and distance from ATM.
- Light and dark themes, mobile-responsive navigation and CSV exports.

## Data integrity and limitations

- No mock or synthetic prices are generated.
- If the token is missing, invalid or the provider fails, the API returns UNAVAILABLE (or STALE only when a prior verified snapshot for that expiry exists).
- The provider's read-only market-data token is tied to an Upstox account. This is not a broker-free public feed, but it has no separate API subscription charge according to Upstox's current Analytics Token documentation.
- The option-chain API is rate-limited by the provider. Do not poll it more frequently than the documented limit.
- The source-received time is not an exchange tick timestamp.
- Signals are screening heuristics, not trained or validated predictions. Candle-confirmed entry triggers, target/stop trigger timestamps and win-rate statistics are not fabricated.

Max Pain is calculated by testing each available strike as a settlement price and summing estimated intrinsic payouts weighted by current CE/PE open interest. It is a positioning reference, not a forecast.

## Run locally (Python 3.12)

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
$env:UPSTOX_ACCESS_TOKEN="paste-token-here"
uvicorn app:app --reload
```

Open http://127.0.0.1:8000

Research tool only. Not investment advice.