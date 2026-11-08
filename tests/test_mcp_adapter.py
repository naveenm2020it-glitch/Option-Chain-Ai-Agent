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
