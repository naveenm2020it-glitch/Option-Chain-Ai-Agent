# NIFTY Option-Chain Data Source Audit

**Audit date:** 2026-11-08 (IST)  
**Repository:** `naveenm2020it-glitch/Option-Chain-Ai-Agent`  
**Scope:** NIFTY 50 option-chain data only; no broker login/API, paid feed, API key, CAPTCHA bypass, anti-bot evasion, or fabricated data.

## Executive result

**No source found in this audit currently satisfies every mandatory requirement for a compliant, free, no-key, non-broker, automated NIFTY live option-chain feed.**

The public NSE MCP service is genuine and no-auth, but its published scope is cash-market data and EOD history; its intraday equity data is described as 1–3 minutes delayed. It does not advertise NIFTY derivatives option-chain tools and explicitly says it is not intended for real-time trading or commercial products.

The NSE website exposes option-chain data to its own website, and recent open-source projects reference the following internal JSON endpoints:

- `GET https://www.nseindia.com/api/option-chain-contract-info?symbol=NIFTY`
- `GET https://www.nseindia.com/api/option-chain-v3?type=Indices&symbol=NIFTY&expiry=DD-Mon-YYYY`

These are **undocumented website endpoints**, not a documented public developer API or a guaranteed service. The legacy endpoint previously used by this project, `/api/option-chain-indices?symbol=NIFTY`, returned HTTP 404 when retrieved by the web research environment on the audit date. That result is not a market-hours integration test.

Crucially, NSE's current website Terms of Use prohibit systematic or automated data collection (including scraping, data mining, extraction and harvesting) and prohibit copying/aggregating website content without permission. NSE's market-data policy describes subscriber agreements and pricing for market-data access. Therefore, this audit does **not** recommend implementing a polling scraper against the website endpoints, rotating user agents, impersonating browser TLS fingerprints, or otherwise attempting to work around access controls.

This is a genuine feasibility limitation, not a coding problem. A source that is technically reachable is not automatically an authorized source for a continuously polled trading dashboard.

## Source comparison

| Source / project | What it actually provides | Auth / cost | NIFTY option chain | Audit result |
|---|---|---|---|---|
| Official NSE public MCP: `https://mcp.nseindia.in/bhavcopy/cm/mcp` and `https://mcp.nseindia.in/cmmkt/mcp` | NSE cash-market tools, bhavcopy history and intraday cash-market information; published delay is 1–3 minutes | No auth; advertised free | No option-chain/derivatives tool advertised | `PARTIAL_DATA` — genuine official service, wrong instrument/scope; not for real-time trading |
| NSE website JSON V3 endpoints | Website option-chain/expiry JSON | No API key advertised; undocumented website endpoints | Yes, technically referenced by current open-source clients | `ACCESS_RESTRICTED` for this automated polling use absent permission; not an officially documented API |
| NSE legacy `option-chain-indices` endpoint | Older website option-chain response shape | No API key advertised | Historically yes | `UNAVAILABLE` in this audit retrieval (HTTP 404); do not build against it |
| `devag7/Indian-Option-MCP` | MCP wrapper with NSE provider, calculations and optional Zerodha provider | MIT code; NSE mode claims no key; Zerodha mode requires credentials | README claims yes | `NOT_TESTED` live. Source uses NSE session-cookie acquisition, rotating User-Agent strings and a 3-second request gap; this does not create an independent licensed feed and is unsuitable under the no-evasion constraint |
| `kbizme/nsemine` | Python wrapper for NSE website data, including option chains | MIT code; no API key advertised | Yes, README documents NIFTY chain | `NOT_TESTED` live. README itself warns users to comply with source terms and avoid excessive scraping; uses curl/TLS impersonation techniques described by its README |
| `Prasad1612/NseKit-MCP` / `Prasad1612/NseKit` | MCP/Python wrapper over NSE data, including F&O option-chain function | Repository code; no key advertised | Yes, README advertises it | `NOT_TESTED` live. It is an NSE website wrapper, not a separate market-data licence/feed |
| `jugaad-py/jugaad-data` | Python NSE wrapper; changelog records migration to V3 and contract-info endpoints | Open-source package; no key advertised | Yes, technically | `NOT_TESTED` live. Still relies on NSE website endpoints; not an independent authorized feed |
| Groww Trade API | Option chain and Greeks | Bearer access token; account/API access | Yes | `AUTH_REQUIRED` — explicitly excluded |
| Upstox API | Put/call option chain | Bearer access token | Yes | `AUTH_REQUIRED` — broker API excluded |
| Flattrade PiConnect | Option-chain endpoint | Login/session key and user ID | Yes | `AUTH_REQUIRED` — broker API excluded |
| Stolo developer API | Live and historical options data | API key / account credentials; plan-dependent | Yes | `AUTH_REQUIRED` / `PAID_REQUIRED` — excluded |
| AFI Codes | Documented NIFTY/BANKNIFTY EOD options endpoint | API key; limited quota / access plan | EOD only | `PAID_REQUIRED` or key-gated; not live intraday |
| NSE Data & Analytics snapshot products | Official F&O snapshot/delayed products | Commercial subscription / agreement | Yes | `PAID_REQUIRED` |

Repository links:

- https://github.com/devag7/Indian-Option-MCP
- https://github.com/kbizme/nsemine
- https://github.com/Prasad1612/NseKit-MCP
- https://github.com/Prasad1612/NseKit
- https://github.com/jugaad-py/jugaad-data

## Official NSE MCP: important distinction

The official NSE MCP page advertises two services:

- Bhavcopy MCP: historical daily data.
- CM Market MCP: cash-market information, described as approximately 1–3 minutes behind real time.

The page says there is no API key or registration, but it also says there is no tick-by-tick or order-book data and that the data is for informational/learning use, not real-time trading or commercial deployment. No NIFTY option-chain tool is listed. It must not be presented as an option-chain provider.

## Endpoint and repository inspection notes

### Current project

The existing `app.py` calls:

```text
GET https://www.nseindia.com/api/option-chain-indices?symbol=NIFTY
```

It first requests the NSE homepage to obtain a session, then parses `records.data`. The repository README already acknowledges that the endpoint is not a guaranteed supported market-data API and can be blocked or changed.

The legacy endpoint should not be treated as a reliable live feed. A successful HTTP response alone would not justify a `LIVE` label; the application needs source timestamp, freshness checks, expiry consistency, and field validation.

### V3 endpoint references

The current V3 route appears in recent open-source code and in the `jugaad-data` changelog. It is useful as a technical research finding, but its existence does not override NSE website terms or make it a supported public API.

No live request was made against the V3 endpoint in this audit because the exchange was closed and the website terms prohibit systematic automated collection without permission. No response sample, update interval, or cloud availability is claimed.

### Indian-Option-MCP

The repository is MIT-licensed as code. Its NSE provider source includes homepage session-cookie acquisition, a pool of rotating User-Agent strings, request serialization and retry/backoff. Its README also advertises an optional Zerodha provider. The project is not itself an independent data source; it wraps NSE website data. The README's live/24-hour claims were not independently verified.

### nsemine

The repository README declares MIT licensing for the software and documents NIFTY option-chain access. It describes `curl_cffi` Chrome TLS fingerprint impersonation and explicitly places source terms and usage compliance responsibility on the user. It is not a licensed independent market-data vendor.

### NseKit-MCP

The MCP server delegates to the NseKit library and advertises `fno_live_option_chain("NIFTY", ...)`. This is another software interface to NSE data, not a separate feed. Its claimed request throttling does not grant permission to automate collection from NSE's website.

### jugaad-data

The GitHub changelog search result records a migration from older option-chain paths to `option-chain-v3` and addition of `option_chain_contract_info()`. This confirms that old integrations have become stale, but does not establish authorization or current service-level reliability.

## Live verification status

Audit time: Sunday, 2026-11-08, approximately 18:58 IST. NSE derivatives market was closed.

| Required test | Result |
|---|---|
| Live NIFTY spot during market hours | `NOT_TESTED` |
| Live expiry list during market hours | `NOT_TESTED` |
| Live V3 option-chain response | `NOT_TESTED` |
| CE/PE LTP, OI, change-OI, volume | `NOT_TESTED` |
| IV and bid/ask | `NOT_TESTED` |
| Source timestamp progression | `NOT_TESTED` |
| Repeated-request freshness | `NOT_TESTED` |
| Render-host connectivity | `NOT_TESTED` |
| Windows Python 3.12 connectivity | `NOT_TESTED` |
| Recovery and failover | `NOT_TESTED` |

The old endpoint's HTTP 404 retrieval is recorded separately; it is not a live-market test. No source is marked `LIVE_VERIFIED`.

## Recommended compliant architecture

The frontend and analytics should remain provider-independent. Do not couple the UI to an NSE-specific response schema.

```text
Authorized provider adapter
        |
        v
Provider response + source timestamp
        |
        v
Schema normalization (CE/PE legs, expiry, spot)
        |
        v
Validation (freshness, duplicate, price, strike, expiry)
        |
        v
Provider health + last-good snapshot
        |
        v
FastAPI JSON endpoint
        |
        +---- SSE/WebSocket to browser
        |
        v
OI / PCR / Max Pain / support-resistance analytics
```

Rules:

1. An adapter may be enabled only after the provider's terms permit the intended use.
2. Keep raw provider timestamps and received timestamps separately.
3. Never mix spot, CE and PE data from different snapshots or expiries.
4. Never promote a source to `LIVE_VERIFIED` on HTTP 200 alone.
5. Missing source fields remain `null`; calculated Greeks must carry a calculation label and inputs.
6. A last-good snapshot may be displayed as `STALE`, never as `LIVE`.
7. Use a single collector with request coalescing, conservative provider-specific rate limits, timeout and exponential backoff.
8. Do not rotate fingerprints, evade CAPTCHA, bypass access controls or replay private session cookies.
9. Use SSE for one-way snapshot updates unless the authorized provider offers a documented WebSocket feed.
10. Keep AI signals disabled unless the underlying market data and the required synchronized spot/premium observations are verified.

## Normalized response contract

```json
{
  "status": "UNAVAILABLE",
  "source": null,
  "source_timestamp": null,
  "received_at": "2026-11-08T18:58:00+05:30",
  "exchange": "NSE",
  "instrument": "NIFTY",
  "spot": null,
  "expiry_dates": [],
  "selected_expiry": null,
  "rows": [],
  "error": "No compliant free no-key NIFTY option-chain provider has been verified."
}
```

The timestamp above is an audit-time example, not a market-data timestamp.

## Decision

Under the user's absolute constraints, **do not select an NSE website scraper or an MCP wrapper around that scraper as the production provider**. The public official NSE MCP is not an option-chain feed. Broker APIs, API-key services, paid vendors and official subscribed market-data products are outside the requested constraints.

A genuinely compliant implementation can proceed only if one of these changes:

- NSE/NSE Data confirms in writing that this specific automated personal-use access is permitted and provides the applicable interface/terms; or
- a third-party provider offers a no-key, free, non-broker NIFTY option-chain feed with terms expressly permitting automated use for this trading application.

Until then, the correct application state is `UNAVAILABLE`, not a fabricated or unverified `LIVE` state.
