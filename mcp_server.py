"""Owned NIFTY MCP server.

Exposes the website's verified market snapshot and deterministic analytics to
MCP-compatible clients. It never invents market data or trade recommendations.
"""
from __future__ import annotations

import os
from typing import Any

from mcp.server.mcpserver import MCPServer

mcp = MCPServer(
    "NIFTY Option Chain AI",
    version="1.0.0",
    instructions=(
        "NIFTY-only option-chain research tools. Market data is returned only "
        "when the website provider's freshness and permission checks pass. "
        "Greeks are model estimates. OI levels and Max Pain are positioning "
        "references, not predictions. No order placement is available."
    ),
)


@mcp.tool()
def get_nifty_market_status() -> dict[str, Any]:
    """Return NIFTY provider status, spot, source timestamp and quality checks."""
    from app import get_market
    snapshot = get_market()
    return {
        "instrument": "NIFTY",
        "status": snapshot.get("status", "UNAVAILABLE"),
        "source": snapshot.get("source"),
        "source_timestamp": snapshot.get("source_timestamp"),
        "received_at": snapshot.get("received_at"),
        "spot": snapshot.get("spot"),
        "expiry_dates": snapshot.get("expiry_dates", []),
        "selected_expiry": snapshot.get("selected_expiry"),
        "quality": snapshot.get("quality", {}),
        "error": snapshot.get("error"),
    }


@mcp.tool()
def get_nifty_option_chain(expiry: str | None = None) -> dict[str, Any]:
    """Return the selected NIFTY expiry option chain only when the snapshot is verified LIVE."""
    from app import get_market
    snapshot = get_market(expiry=expiry)
    if snapshot.get("status") != "LIVE":
        return {
            "instrument": "NIFTY",
            "status": snapshot.get("status", "UNAVAILABLE"),
            "source_timestamp": snapshot.get("source_timestamp"),
            "received_at": snapshot.get("received_at"),
            "quality": snapshot.get("quality", {}),
            "rows": [],
            "error": snapshot.get("error") or "Verified live option-chain data is unavailable.",
        }
    chosen = expiry if expiry in snapshot.get("expiry_dates", []) else snapshot.get("selected_expiry")
    rows = [row for row in snapshot.get("rows", []) if row.get("expiry") == chosen]
    return {
        "instrument": "NIFTY",
        "status": "LIVE",
        "source": snapshot.get("source"),
        "source_timestamp": snapshot.get("source_timestamp"),
        "received_at": snapshot.get("received_at"),
        "spot": snapshot.get("spot"),
        "expiry": chosen,
        "rows": rows,
        "quality": snapshot.get("quality", {}),
    }


@mcp.tool()
def analyze_nifty_option_chain(expiry: str | None = None, top_n: int = 5) -> dict[str, Any]:
    """Calculate Max Pain, PCR, OI/change-OI support and resistance candidates for one expiry."""
    from app import get_market
    from services.oi_analytics import analyze_option_chain
    snapshot = get_market(expiry=expiry)
    if snapshot.get("status") != "LIVE":
        return {
            "status": "WAIT",
            "instrument": "NIFTY",
            "expiry": expiry,
            "metrics": None,
            "error": snapshot.get("error") or "Verified live market data is unavailable.",
            "source_timestamp": snapshot.get("source_timestamp"),
            "quality": snapshot.get("quality", {}),
        }
    chosen = expiry if expiry in snapshot.get("expiry_dates", []) else snapshot.get("selected_expiry")
    rows = [row for row in snapshot.get("rows", []) if row.get("expiry") == chosen]
    result = analyze_option_chain(rows, top_n=max(1, min(int(top_n), 20)))
    result.update({
        "instrument": "NIFTY",
        "spot": snapshot.get("spot"),
        "source": snapshot.get("source"),
        "source_timestamp": snapshot.get("source_timestamp"),
        "quality": snapshot.get("quality", {}),
    })
    return result


@mcp.tool()
def calculate_option_greeks(
    spot: float,
    strike: float,
    time_to_expiry_years: float,
    implied_volatility: float,
    option_type: str,
    risk_free_rate: float = 0.065,
    dividend_yield: float = 0.0,
) -> dict[str, Any]:
    """Calculate Black-Scholes Greeks; rates and IV are annual decimals (0.20 = 20% IV)."""
    from services.greeks import calculate_greeks
    result = calculate_greeks(
        spot=spot,
        strike=strike,
        time_to_expiry_years=time_to_expiry_years,
        implied_volatility=implied_volatility,
        option_type=option_type,
        risk_free_rate=risk_free_rate,
        dividend_yield=dividend_yield,
        iv_source="caller_supplied",
    )
    return result.as_dict()


@mcp.tool()
def get_nifty_signal_policy() -> dict[str, Any]:
    """Return the configured research signal constraints; this does not generate a trade signal."""
    return {
        "instrument": "NIFTY",
        "allowed_sides": ["CE", "PE"],
        "target_premium_points": 20,
        "stop_loss_premium_points": 10,
        "maximum_horizon_seconds": 900,
        "price_observation": "successive valid option-chain snapshots and selected contract LTP",
        "premium_candles_required": False,
        "ambiguous_snapshot_gap": "UNRESOLVED",
        "automatic_order_placement": False,
        "signal_engine_status": "SNAPSHOT_CONFLUENCE_HEURISTIC_IMPLEMENTED",\n        "entry_rule": "Requires spot direction, option premium movement, OI/change-OI, liquidity spread, model delta alignment and theta risk checks.",
        "data_policy": "Never generate a LIVE signal from stale, unavailable, incomplete or unapproved data.",
    }


@mcp.tool()
def get_recent_nifty_analyses(limit: int = 20) -> dict[str, Any]:
    """Return recent persisted analysis decisions (WAIT decisions included)."""
    from storage.database import ResearchDatabase
    db = ResearchDatabase(os.getenv("NIFTY_DATABASE_PATH", "data/nifty_research.sqlite3"))
    db.initialize()
    records = db.list_analyses(limit=max(1, min(int(limit), 100)), offset=0)
    return {"instrument": "NIFTY", "count": len(records), "items": records}



@mcp.tool()
def get_nifty_greeks_matrix(expiry: str | None = None) -> dict[str, Any]:
    """Return model-calculated Greeks for available CE/PE contracts in one NIFTY expiry."""
    from app import greeks
    return greeks(expiry=expiry)


@mcp.tool()
def get_nifty_signal_journal(limit: int = 50) -> dict[str, Any]:
    """Return the latest NIFTY signal lifecycle records and their outcomes."""
    from storage.database import ResearchDatabase
    db = ResearchDatabase(os.getenv("NIFTY_DATABASE_PATH", "data/nifty_research.sqlite3"))
    db.initialize()
    items = db.list_signals(limit=max(1, min(int(limit), 200)))
    return {"instrument": "NIFTY", "count": len(items), "items": items}


@mcp.tool()
def get_nifty_performance() -> dict[str, Any]:
    """Return observed target/stop outcomes; does not claim predictive validation."""
    from storage.database import ResearchDatabase
    db = ResearchDatabase(os.getenv("NIFTY_DATABASE_PATH", "data/nifty_research.sqlite3"))
    db.initialize()
    return db.performance_summary()


class BearerTokenASGI:
    """Protect the public MCP endpoint with a constant-time bearer-token check."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token = os.getenv("MCP_ACCESS_TOKEN", "").strip()
        if not token:
            await self._respond(send, 503, b'{"error":"MCP endpoint disabled: configure MCP_ACCESS_TOKEN."}')
            return
        import hmac
        headers = dict(scope.get("headers", []))
        supplied = headers.get(b"authorization", b"").decode("latin1")
        expected = "Bearer " + token
        if not hmac.compare_digest(supplied, expected):
            await self._respond(send, 401, b'{"error":"Unauthorized"}')
            return
        await self.app(scope, receive, send)

    @staticmethod
    async def _respond(send, status: int, body: bytes):
        await send({
            "type": "http.response.start",
            "status": status,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        })
        await send({"type": "http.response.body", "body": body})


def protected_mcp_app():
    """Build the authenticated Streamable HTTP MCP ASGI application."""
    return BearerTokenASGI(mcp.streamable_http_app(json_response=True, stateless_http=True))
