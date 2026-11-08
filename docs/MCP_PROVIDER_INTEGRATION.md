# MCP Provider Integration Plan — NIFTY Option Chain AI

**Status:** Included in architecture; candidate evaluation and implementation pending  
**Date:** 2026-11-08  
**Repository:** `naveenm2020it-glitch/Option-Chain-Ai-Agent`

## 1. Purpose

MCP (Model Context Protocol) is an integration protocol. It is not itself a market-data feed and does not grant permission to access, automate, store or redistribute exchange data. An MCP server is useful only when its underlying source is suitable, its data is genuine and sufficiently fresh, and its terms permit the intended use.

MCP support is now part of the NIFTY Option Chain AI architecture. It must not bypass the project's source-authorization and data-quality gates.

## 2. MCP candidates to evaluate

| Candidate | Intended evaluation | Current limitation / decision |
|---|---|---|
| Official NSE MCP | Inspect available tools and use for supported NSE information, where permitted. | The reviewed tool set did not expose NIFTY derivatives option-chain data. Do not treat it as the option-chain provider unless this capability is verifiably added. |
| Indian-Option-MCP | Evaluate its NIFTY option-chain response, CE/PE fields, expiry handling, timestamps and operational reliability. | It relies on unofficial NSE website endpoints. Repository availability or a successful response does not establish NSE authorization, stable API access or redistribution rights. Candidate only; disabled by default. |
| India Market MCP | Evaluate advertised NIFTY option-chain and related analytics fields, including source timestamp and freshness. | Treat claims about cache interval, delay, data completeness and permissions as unverified until tested against source code, documentation and market-hours observations. Candidate only; disabled by default. |
| Custom MCP adapter | Provide a standard MCP-facing interface for a future approved provider, or expose this app's own normalized analytics to an MCP client. | This adapter is an integration layer, not a new source of market data and cannot make an unauthorized source acceptable. |

Candidate references:
- Official NSE MCP: use the official NSE MCP documentation and tool catalogue.
- Indian-Option-MCP: https://github.com search for the exact project repository and inspect its current README, source, license and upstream endpoints before pinning a dependency.
- India Market MCP: https://github.com search for the exact project repository and inspect its current README, source, license and upstream endpoints before pinning a dependency.

Do not hard-code unverified repository URLs, package names, tool names or MCP method names. Record the exact repository URL, commit/tag, license, upstream endpoint list and review date in the provider audit when evaluation is performed.

### Reviewed candidate: kcodweb/india-market-mcp

Repository: https://github.com/kcodweb/india-market-mcp  
Read-only tool: `get_option_chain(symbol, expiry, strikes_each_side, rate_pct)`  
Example invocation: `symbol=NIFTY`, `expiry=nearest`, `strikes_each_side=50`, `rate_pct=6.5`.

The tool returns `underlying_price`, `expiry`, `expiries`, `as_of`, a compact `chain` array (call/put LTP, change, OI, change-OI, volume, IV, bid/ask and delta), and summary analytics. The app adapter maps this response to its internal CE/PE snapshot schema and preserves the source timestamp; it does not fabricate a timestamp when missing.

The candidate README identifies its option-chain source as NSE website APIs, describes the endpoints as unofficial and not a licensed data feed, and documents browser-like headers, session cookies, refresh after 401/403, request spacing and a 20-second cache. It also describes the data as a few minutes delayed. A MIT license on the MCP server code does not grant rights to NSE data. These are material limitations for a 15-minute option-buying signal: the expected delay may exceed the freshness limit and make the source unsuitable for entry alerts. Therefore this candidate is wired only as an optional, disabled-by-default technical integration; it is not approved for production automation. Do not copy its upstream collection/access-handling implementation into this project.

## 3. Proposed runtime architecture

```text
Candidate MCP server (only after source review)
             |
             v
MCP transport client / adapter
(stdio for local process OR supported HTTP transport for hosted service)
             |
             v
Provider-neutral normalized snapshot
             |
             v
Authorization + capability gate
             |
             v
Data-quality gate (timestamp, freshness, NIFTY, expiry, spot, CE/PE)
             |
             v
Existing analytics / AI / signal lifecycle / journal
```

The core analytics engine must not depend on MCP-specific response shapes. Each MCP candidate gets its own adapter implementing the existing provider contract. Preserve raw payloads, source timestamps, receive timestamps, tool/server identity and adapter version for auditability.

## 4. Render deployment considerations

- A local MCP server using `stdio` is a child process, not an ordinary HTTP endpoint. Render web services cannot call it as if it were a REST URL; the backend must manage the process and MCP client lifecycle.
- For a hosted MCP server, use only a documented and supported transport (for example, a supported HTTP streaming transport). Do not assume a repository's transport from its name.
- Keep MCP credentials out of source control. The project still prefers no-key/no-paid providers; do not introduce credentials unless the user explicitly changes that requirement.
- Use one backend collector and bounded polling. Do not let every dashboard browser tab create its own upstream MCP session or repeated exchange requests.
- Add timeouts, cancellation, reconnect backoff, health status and graceful shutdown.
- Do not expose an arbitrary MCP tool execution endpoint to the public internet. Allowlist only the specific read-only tools required by the NIFTY provider adapter.

## 5. Mandatory acceptance checks

An MCP candidate remains `DISABLED` until all applicable checks pass:

1. **Source and permission:** identify the actual upstream data source; review its terms, repository license and any exchange restrictions for automated access, storage and display.
2. **Capabilities:** confirm it returns NIFTY spot, expiry, CE and PE contracts, strike, LTP, OI, change in OI and volume. Greeks/IV/bid/ask may be optional only when the app can safely calculate or mark them unavailable.
3. **Provenance:** capture the source's own timestamp, not merely the MCP response/receive time. If no trustworthy source timestamp exists, do not call the data LIVE.
4. **Freshness:** measure timestamp age and progression during actual NSE market hours. A 200 response, non-empty chain or recent local receive time is insufficient.
5. **Coherence:** spot, expiry and all chain rows must belong to one coherent observation; reject mixed expiries, duplicate strikes and partial CE/PE rows according to the quality policy.
6. **Stability:** test repeated requests, rate limits, outages, schema changes, reconnects and Render cold starts.
7. **Reproducibility:** store sanitized test fixtures and the exact MCP server version/commit; never present fixtures as live market data.
8. **No evasion:** no CAPTCHA bypass, cookie replay, browser fingerprint rotation, proxy rotation or other access-control circumvention.

If permissions are unclear or prohibited, do not enable the provider for automated production collection. If source timestamp or quality checks fail, return `STALE` or `UNAVAILABLE`, pause analysis/alerts and retain the reason.

## 6. Provider selection and fallback policy

- MCP is an optional transport/integration path, not a privileged bypass around the provider contract.
- Do not automatically fail over between sources unless both are independently approved and their timestamps/expiry/schema are compatible.
- Do not merge spot from one provider with option-chain rows from another provider into a single snapshot.
- Keep provider priority explicit in configuration and display the actual source name.
- Until a candidate passes the acceptance checks, the production status remains `UNAVAILABLE`; no live alerts or performance claims are allowed.

## 7. Implementation tasks

- [ ] Inspect the official NSE MCP tool catalogue and document its actual supported capabilities.
- [ ] Inspect the exact Indian-Option-MCP repository, license, upstream endpoints, transport and schema.
- [ ] Inspect the exact India Market MCP repository, license, upstream endpoints, transport and schema.
- [ ] Build a read-only MCP adapter interface behind `providers/base.py`.
- [ ] Add fixture-based adapter tests for valid, stale, malformed and partial snapshots.
- [ ] Add integration tests for transport failure, timeout, reconnect and shutdown.
- [ ] Run market-hours freshness and consistency tests before any provider is enabled.
- [ ] Confirm Render transport/process compatibility and operational limits.
- [ ] Enable a candidate only after authorization and every required acceptance check is documented.

**Important:** MCP integration does not change the user's other fixed requirements: NIFTY only; no broker API or order placement; no paid feed or API key; no fabricated data; no premium-candle dependency; entry/target/stop and outcome tracking from validated option-chain snapshots and exact-contract LTP observations.
