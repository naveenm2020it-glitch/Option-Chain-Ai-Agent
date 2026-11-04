# Option Chain AI Agent

Responsive NIFTY option-chain research and paper-trading web app. This repository is separate from NIFTY-Labs.

## Included in v0.1
- Responsive dashboard, option-chain view, AI watchlist, paper terminal, journal and analytics.
- NSE website public-endpoint adapter with LIVE / STALE / UNAVAILABLE states.
- ATM ±10 strikes (50-point strike spacing), nearest available expiry.
- Transparent heuristic ranking based on premium change, volume, open interest and bid/ask spread.
- Paper trade API: one open position, +20 option-premium-point target, −10 point stop, automatic target/stop checks while live data is healthy.
- SQLite trade journal; no broker integration or real orders.

## Critical limitations
NSE website endpoints are not a guaranteed supported market-data API. They may rate-limit, change or block cloud-hosted requests. A deployment does not guarantee live data. The app must display UNAVAILABLE/STALE and pause execution if data cannot be verified. Use an authorized market-data provider for dependable production feeds.

The current score is a rules-based screening heuristic, not a trained AI model. It does not include spot/premium candle confirmation, so candidates are labelled WATCH and automatic entries are disabled. Do not treat scores as buy recommendations.

## Run locally (Python 3.12)
```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
uvicorn app:app --reload
```
Open http://127.0.0.1:8000

## Render
A render.yaml Blueprint is included. In Render select New + -> Blueprint, connect this repository and Apply. Free Render filesystems are ephemeral and services may spin down; journal persistence requires a persistent disk or managed database.

## Accounting
P&L excludes brokerage, exchange charges, GST, STT, stamp duty, slippage and taxes. This is a research simulator, not investment advice.
