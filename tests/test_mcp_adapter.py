import json

import pytest

from providers.mcp_adapter import (
    MCPOptionChainProvider,
    MCPProviderError,
    _extract_snapshot,
)


class TextBlock:
    def __init__(self, text):
        self.text = text


class ToolResult:
    isError = False
    structuredContent = None

    def __init__(self, text):
        self.content = [TextBlock(text)]


def test_mcp_adapter_is_disabled_by_default():
    provider = MCPOptionChainProvider({
        "enabled": False,
        "automated_use_permitted": False,
        "command": "",
        "args": [],
        "tool_name": "",
        "tool_args": {},
        "timeout_seconds": 10,
    })
    allowed, reason = provider.configuration_status()
    assert allowed is False
    assert "disabled" in reason


def test_mcp_adapter_requires_explicit_permission():
    provider = MCPOptionChainProvider({
        "enabled": True,
        "automated_use_permitted": False,
        "command": "python",
        "args": ["server.py"],
        "tool_name": "get_chain",
        "tool_args": {},
        "timeout_seconds": 10,
    })
    allowed, reason = provider.configuration_status()
    assert allowed is False
    assert "not approved" in reason


def test_mcp_adapter_requires_configured_tool_and_command():
    provider = MCPOptionChainProvider({
        "enabled": True,
        "automated_use_permitted": True,
        "command": "",
        "args": [],
        "tool_name": "",
        "tool_args": {},
        "timeout_seconds": 10,
    })
    allowed, reason = provider.configuration_status()
    assert allowed is False
    assert "required" in reason


def test_extract_snapshot_from_json_text():
    payload = {"instrument": "NIFTY", "spot": 24000, "rows": []}
    result = _extract_snapshot(ToolResult(json.dumps(payload)))
    assert result == payload


def test_extract_snapshot_accepts_snapshot_wrapper():
    payload = {"snapshot": {"instrument": "NIFTY", "spot": 24000}}
    result = _extract_snapshot(ToolResult(json.dumps(payload)))
    assert result["spot"] == 24000


def test_extract_snapshot_rejects_non_json_tool_output():
    with pytest.raises(MCPProviderError):
        _extract_snapshot(ToolResult("connected successfully"))


def test_normalize_india_market_mcp_chain_shape():
    from providers.mcp_adapter import _normalize_india_market_mcp

    payload = {
        "symbol": "NIFTY",
        "underlying_price": 24100.0,
        "expiry": "2026-11-12",
        "expiries": ["2026-11-12", "2026-11-19"],
        "as_of": "09-Nov-2026 10:15:00",
        "chain": [{
            "strike": 24100,
            "call": {"ltp": 100.0, "oi": 1000, "chg_oi": 50, "volume": 200, "iv": 12.5, "bid": 99.5, "ask": 100.5, "delta": 0.5},
            "put": {"ltp": 95.0, "oi": 1200, "chg_oi": 60, "volume": 220, "iv": 13.0, "bid": 94.5, "ask": 95.5, "delta": -0.5},
        }],
    }
    normalized = _normalize_india_market_mcp(payload)
    assert normalized["instrument"] == "NIFTY"
    assert normalized["spot"] == 24100.0
    assert normalized["selected_expiry"] == "2026-11-12"
    assert normalized["source_timestamp"].endswith("+05:30")
    assert normalized["rows"][0]["ce"]["lastPrice"] == 100.0
    assert normalized["rows"][0]["pe"]["changeinOpenInterest"] == 60
