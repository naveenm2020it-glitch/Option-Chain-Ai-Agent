"""Read-only MCP client adapter for normalized NIFTY option-chain snapshots.

The adapter deliberately accepts only a configured, allowlisted MCP tool and
expects that tool to return the application's normalized snapshot contract.
It does not scrape NSE endpoints or infer missing source timestamps.
"""
from __future__ import annotations

import asyncio
import json
import os
import shlex
from datetime import datetime
from typing import Any

from zoneinfo import ZoneInfo

from providers.base import ProviderCapabilities

IST = ZoneInfo("Asia/Kolkata")


class MCPProviderError(RuntimeError):
    """Raised when an MCP server/tool cannot provide a valid snapshot."""


def _truthy(name: str) -> bool:
    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def mcp_configuration() -> dict[str, Any]:
    """Read explicit MCP configuration. Disabled and unapproved by default."""
    command = os.getenv("MCP_SERVER_COMMAND", "").strip()
    tool_name = os.getenv("MCP_OPTION_CHAIN_TOOL", "").strip()
    try:
        tool_args = json.loads(os.getenv("MCP_OPTION_CHAIN_ARGS_JSON", "{}"))
        if not isinstance(tool_args, dict):
            raise ValueError("MCP_OPTION_CHAIN_ARGS_JSON must contain a JSON object.")
    except (json.JSONDecodeError, ValueError) as exc:
        raise MCPProviderError(str(exc)) from exc

    return {
        "enabled": _truthy("MCP_PROVIDER_ENABLED"),
        "automated_use_permitted": _truthy("MCP_AUTOMATED_USE_PERMITTED"),
        "command": command,
        "args": shlex.split(os.getenv("MCP_SERVER_ARGS", "")),
        "tool_name": tool_name,
        "tool_args": tool_args,
        "timeout_seconds": max(2, min(60, int(os.getenv("MCP_TIMEOUT_SECONDS", "15")))),
    }


def _source_timestamp(value: Any) -> str | None:
    """Parse known NSE timestamp forms; never substitute local receive time."""
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=IST)
        return parsed.isoformat(timespec="seconds")
    except ValueError:
        pass
    for fmt in ("%d-%b-%Y %H:%M:%S", "%d-%b-%Y, %H:%M:%S"):
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=IST).isoformat(timespec="seconds")
        except ValueError:
            continue
    return raw


def _normalize_india_market_mcp(payload: dict[str, Any]) -> dict[str, Any]:
    """Map the kcodweb India Market MCP get_option_chain result to our schema."""
    if isinstance(payload.get("rows"), list):
        return payload
    chain = payload.get("chain")
    if not isinstance(chain, list):
        return payload
    expiry = payload.get("expiry")
    rows: list[dict[str, Any]] = []

    def leg(value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        return {
            "lastPrice": value.get("ltp"),
            "change": value.get("chg"),
            "totalTradedVolume": value.get("volume"),
            "openInterest": value.get("oi"),
            "changeinOpenInterest": value.get("chg_oi"),
            "impliedVolatility": value.get("iv"),
            "bidprice": value.get("bid"),
            "askPrice": value.get("ask"),
            "delta": value.get("delta"),
        }

    for item in chain:
        if not isinstance(item, dict):
            continue
        rows.append({
            "strike": item.get("strike"),
            "expiry": expiry,
            "ce": leg(item.get("call")),
            "pe": leg(item.get("put")),
        })
    expiries = payload.get("expiries")
    return {
        "instrument": "NIFTY",
        "source": "India Market MCP (NSE website API)",
        "source_timestamp": _source_timestamp(payload.get("as_of")),
        "received_at": None,
        "spot": payload.get("underlying_price"),
        "expiry_dates": expiries if isinstance(expiries, list) else ([expiry] if expiry else []),
        "selected_expiry": expiry,
        "rows": rows,
        "mcp_analytics": payload.get("summary"),
        "mcp_notes": payload.get("notes"),
    }


def _extract_snapshot(result: Any) -> dict[str, Any]:
    """Extract a JSON object from structuredContent or a text content block."""
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        payload: Any = structured
    else:
        payload = None
        for block in getattr(result, "content", []) or []:
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                continue
            try:
                candidate = json.loads(text)
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
    if not isinstance(payload, dict):
        raise MCPProviderError(
            "Configured MCP tool did not return a JSON object in structuredContent or text."
        )
    if getattr(result, "isError", False):
        raise MCPProviderError("Configured MCP tool returned an MCP error result.")
    # Accept either a direct snapshot or a single explicit snapshot wrapper.
    if isinstance(payload.get("snapshot"), dict):
        payload = payload["snapshot"]
    return _normalize_india_market_mcp(payload)


class MCPOptionChainProvider:
    """Managed stdio MCP client for a single read-only option-chain tool."""

    name = "configured_mcp_option_chain"
    capabilities = ProviderCapabilities(
        instrument="NIFTY",
        option_chain=True,
        spot=True,
        option_greeks=False,
        documented_api=False,
        automated_use_permitted=False,
        requires_credentials=False,
        requires_payment=False,
    )

    def __init__(self, config: dict[str, Any] | None = None):
        self.config = config or mcp_configuration()

    def configuration_status(self) -> tuple[bool, str | None]:
        cfg = self.config
        if not cfg["enabled"]:
            return False, "MCP provider is disabled (MCP_PROVIDER_ENABLED is not true)."
        if not cfg["automated_use_permitted"]:
            return False, (
                "MCP provider is not approved: review the upstream source terms first, "
                "then explicitly set MCP_AUTOMATED_USE_PERMITTED=true."
            )
        if not cfg["command"] or not cfg["tool_name"]:
            return False, "MCP_SERVER_COMMAND and MCP_OPTION_CHAIN_TOOL are required."
        if not cfg["tool_name"].replace("_", "").replace("-", "").isalnum():
            return False, "MCP tool name contains unsupported characters."
        return True, None

    def fetch_snapshot(self, instrument: str = "NIFTY", expiry: str | None = None) -> dict[str, Any]:
        if instrument.upper() != "NIFTY":
            raise MCPProviderError("This MCP adapter is restricted to NIFTY.")
        allowed, reason = self.configuration_status()
        if not allowed:
            raise MCPProviderError(reason or "MCP provider is not enabled.")

        try:
            return asyncio.run(self._fetch_snapshot(instrument.upper(), expiry))
        except RuntimeError as exc:
            # FastAPI's synchronous route normally has no running event loop.
            # Avoid hiding nested-loop misuse as a provider outage.
            raise MCPProviderError(f"MCP event-loop error: {exc}") from exc

    async def _fetch_snapshot(self, instrument: str, expiry: str | None) -> dict[str, Any]:
        try:
            from mcp import ClientSession, StdioServerParameters
            from mcp.client.stdio import stdio_client
        except ImportError as exc:
            raise MCPProviderError(
                "MCP SDK is not installed. Install requirements.txt before enabling MCP."
            ) from exc

        cfg = self.config
        params = dict(cfg["tool_args"])
        params.setdefault("instrument", instrument)
        params.setdefault("symbol", instrument)
        if expiry:
            params.setdefault("expiry", expiry)

        server = StdioServerParameters(
            command=cfg["command"],
            args=cfg["args"],
            env=os.environ.copy(),
        )
        try:
            async with stdio_client(server) as (read_stream, write_stream):
                async with ClientSession(read_stream, write_stream) as session:
                    await asyncio.wait_for(session.initialize(), timeout=cfg["timeout_seconds"])
                    tools_result = await asyncio.wait_for(
                        session.list_tools(), timeout=cfg["timeout_seconds"]
                    )
                    available = {tool.name for tool in tools_result.tools}
                    tool_name = cfg["tool_name"]
                    if tool_name not in available:
                        raise MCPProviderError(
                            f"Configured MCP tool '{tool_name}' is not in the server tool list."
                        )
                    result = await asyncio.wait_for(
                        session.call_tool(tool_name, arguments=params),
                        timeout=cfg["timeout_seconds"],
                    )
                    snapshot = _extract_snapshot(result)
        except asyncio.TimeoutError as exc:
            raise MCPProviderError("MCP request timed out.") from exc
        except MCPProviderError:
            raise
        except Exception as exc:
            raise MCPProviderError(f"MCP transport/tool failure: {type(exc).__name__}: {exc}") from exc

        snapshot.setdefault("instrument", instrument)
        snapshot.setdefault("source", f"MCP:{tool_name}")
        # received_at is local receipt time only; source_timestamp must come
        # from the upstream market-data response and is never synthesized.
        snapshot["received_at"] = datetime.now(IST).isoformat(timespec="seconds")
        return snapshot


def get_mcp_provider() -> MCPOptionChainProvider:
    return MCPOptionChainProvider()
