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
