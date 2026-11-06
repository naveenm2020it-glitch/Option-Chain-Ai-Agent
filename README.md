# NIFTY Option Chain Intelligence

A responsive NIFTY 50 option-chain research terminal with live NSE website endpoint integration.

## Current features
- Live / STALE / UNAVAILABLE source-state handling.
- NIFTY spot, ATM and selected expiry.
- Full selected-expiry CE/PE option chain.
- OI concentration support and resistance levels, PCR and change in OI.
- Max Pain calculation from current open interest and intrinsic payout.
- Rules-based multi-strike watchlist using premium change, OI change, volume, bid/ask spread and distance from ATM.
- Light and dark themes, mobile-responsive navigation and CSV exports.

## Data integrity and limitations
NSE website public endpoints are not a guaranteed supported market-data API. Requests may be rate-limited, changed or blocked, especially from cloud hosting. The UI must show LIVE, STALE or UNAVAILABLE honestly; deployment does not guarantee live market data. For dependable production use, connect an authorized market-data provider.

The strike ranking is a transparent rules-based screening heuristic, not a trained or validated AI model. It is not an entry recommendation. The current adapter does not provide synchronized spot and option-premium candles, so candle-confirmed entry triggers, target/stop trigger timestamps and win-rate statistics are deliberately not fabricated. The alerts screen reports this limitation.

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
The repository includes a Render Blueprint. The existing Render web service can deploy from the connected GitHub main branch. Free Render instances may spin down; the NSE endpoint can also block cloud requests.

Research tool only. Not investment advice.


## Free upstream reference and data limitations

This project references the MIT-licensed [Indian Markets Options Hub](https://github.com/yash-sarawgi/Indian-Markets-Options-Hub) for its browser-based NSE F&O analytics approach. Its MIT licence permits reuse subject to retaining required copyright and licence notices. This project does not claim ownership of upstream code.

The no-subscription data path uses the public NSE website endpoint on a best-effort basis. It is not a guaranteed supported market-data API and can be rate-limited or blocked, especially from cloud hosting. The interface must show LIVE only after a successful fresh response; otherwise it shows STALE or UNAVAILABLE. Free source code does not make a paid broker feed free.

Greeks & Premium Intelligence displays Black–Scholes estimates derived from the option-chain IV field (6.5% risk-free-rate assumption). These are model estimates, not exchange-published or broker-provided Greeks. No entry/exit alerts or accuracy claims are generated without synchronized option and spot candles.

## Experimental: Indian-Option-MCP evaluation

This branch adds a **Data Source Test** screen and the endpoint
`GET /api/providers/indian-option-mcp/test`.

The upstream [devag7/Indian-Option-MCP](https://github.com/devag7/Indian-Option-MCP)
project is a Node.js MCP server that documents NSE as its default provider and
Zerodha as an optional provider. NIFTY LAB does not enable or configure the
Zerodha provider.

Important architecture distinction: the upstream project is an MCP server using
stdio transport, not a ready-made HTTP market-data API. This experiment tests
the same primary NSE option-chain endpoint independently from the NIFTY LAB
web process so its response can be inspected before any production integration.

### Test behaviour

- One normal NSE homepage session request followed by one option-chain request.
- No rotating user-agent identities.
- No retry after HTTP 403 or 429.
- No alternate endpoint fallback after an access denial.
- Reports HTTP status, exchange timestamp, NIFTY spot, expiry count, CE/PE row
  counts and request duration.
- Does not fabricate data. A successful HTTP response confirms connectivity
  only; it does not prove an NSE data licence or continuous availability.

The upstream repository describes its NSE provider as a scraper. NSE website
terms and data policies may restrict automated collection and redistribution.
This branch is an isolated technical evaluation, not a statement that this
scraping method is authorised for production trading use.

### How to test

After deploying this branch to a separate test service, open **Data Source Test**
in the sidebar and tap **Test connection**. Or call:

```text
GET /api/providers/indian-option-mcp/test
```

The existing Render production service is not changed by this branch.

