# Production backend architecture

## Active deployment profile

- Runtime: Python 3.12 / FastAPI on Render.
- Instrument: NIFTY 50 options only.
- Execution: disabled; this service is a read-only research and alerting terminal.
- NSE public website: manual viewing and user-downloaded CSV import only.
- Automated NSE website JSON collection: disabled and not implemented.
- Broker providers: disabled in the Render deployment profile.
- Live status: fail-closed until an approved provider returns a valid, fresh snapshot.

## Data flow

1. A future approved provider implements the normalized provider contract in `providers/base.py`.
2. The provider returns NIFTY spot, one selected expiry, CE/PE rows, a genuine timezone-aware source timestamp, and a receive timestamp.
3. `services/data_quality.py` validates source permission, timestamps, spot, unique strikes, expiry consistency, CE/PE quotes and freshness.
4. Only a snapshot that passes validation may be marked LIVE.
5. The analytics layer calculates ATM, PCR, OI concentration, Max Pain and Greeks where source IV is available.
6. The signal engine evaluates a new snapshot only when its source timestamp is new. Duplicate snapshots do not count as new evidence.
7. The backend publishes provider status and source timestamp changes through `/ws/market`.
8. The browser updates its display when it receives a new provider event. REST polling is retained only as a low-frequency fallback if the WebSocket is disconnected.

## Timing contract

- `ANALYTICS_UPDATE_INTERVAL_SECONDS=3`: WebSocket heartbeat / dashboard responsiveness target.
- `PROVIDER_POLL_INTERVAL_SECONDS=5`: separate backend provider polling cadence for a provider that explicitly requires polling.
- `PROVIDER_CACHE_TTL_SECONDS=5`: shares provider snapshots across API requests.
- A provider with its own WebSocket should push ticks into the provider adapter; it should not be polled every three seconds just to satisfy the UI target.
- A heartbeat is not a market tick. The app must not update prices, timestamps, analytics, or signal outcomes from heartbeat messages.
- Analytics and signal lifecycle evaluation happen only on a new, unique, permitted, fresh source snapshot. There is no guarantee that the source itself emits new data every three seconds.

## WebSocket messages

Endpoint: `/ws/market`

Connection message:
```json
{
  "type": "connection",
  "status": "CONNECTED",
  "market_data_status": "UNAVAILABLE"
}
```

Heartbeat message (transport health only):
```json
{
  "type": "heartbeat",
  "market_data_status": "UNCHANGED"
}
```

Provider update message:
```json
{
  "type": "market_update",
  "status": "LIVE",
  "source": "approved-provider",
  "source_timestamp": "2026-11-09T10:15:03+05:30",
  "received_at": "2026-11-09T10:15:03+05:30",
  "quality": {}
}
```

The values above are schema examples only, not actual market data. The browser fetches the full normalized snapshot through the existing REST analysis endpoint after a new source timestamp is announced. No example or heartbeat is ever presented as live market data.

## NSE operating mode

The Render environment explicitly sets:
- `NSE_ENABLED=false`
- `NSE_AUTOMATED_COLLECTION_ENABLED=false`
- `MCP_PROVIDER_ENABLED=false`
- `MCP_AUTOMATED_USE_PERMITTED=false`
- `JUGAAD_PROVIDER_ENABLED=false`
- `JUGAAD_AUTOMATED_USE_PERMITTED=false`
- `FYERS_PROVIDER_ENABLED=false`
- `ORDER_EXECUTION_ENABLED=false`

The manual NSE CSV import is retained for user-initiated research. It is never labelled LIVE and is not fed into the live entry-signal lifecycle.

## Entry and safety controls

- Target: +20 option premium points.
- Stop: -10 option premium points.
- Maximum monitoring horizon: 15 minutes.
- No order placement, modification, cancellation or broker execution calls.
- Entry alerts remain paused for UNAVAILABLE, UNVERIFIED, STALE or DISCONNECTED data.
- Missing, stale, malformed, mixed-expiry or duplicate snapshots fail closed.
- Signal outcomes with ambiguous target/stop ordering across a data gap remain UNRESOLVED rather than being guessed.

## Render deployment notes

1. Deploy the FastAPI service using the repository's `render.yaml`.
2. Verify `/api/health` reports `order_execution_enabled=false`, `nse_automated_collection_enabled=false`, and the configured timing values.
3. Verify `/ws/market` accepts a WebSocket connection and sends heartbeats.
4. With the active NSE-only profile, the expected market status is UNAVAILABLE; this is intentional until an approved feed is configured.
5. Do not enable a provider by setting an environment variable until its exact upstream source, license, automated-use permission, display/storage rights, fields, timestamp semantics and rate limits have been reviewed.
6. Do not treat Render free-tier availability or SQLite local storage as production durability guarantees. Use a persistent database before relying on long-term signal records.

## Acceptance criteria

- No automatic NSE website endpoint requests.
- No broker API enabled in the active deployment.
- No order execution path.
- WebSocket heartbeat does not alter market values.
- A changed source timestamp triggers a dashboard refresh.
- Only permitted, fresh, structurally valid source snapshots can enter analytics.
- The UI clearly shows UNAVAILABLE while no approved live provider exists.
- A new source adapter can be added without changing the analytics and UI contracts.
