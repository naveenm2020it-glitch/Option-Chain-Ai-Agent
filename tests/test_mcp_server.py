import asyncio


import app as website_app  # noqa: F401 - verify website app mounts MCP
from mcp_server import BearerTokenASGI, get_nifty_signal_policy


def test_signal_policy_matches_user_constraints():
    policy = get_nifty_signal_policy()
    assert policy["target_premium_points"] == 20
    assert policy["stop_loss_premium_points"] == 10
    assert policy["maximum_horizon_seconds"] == 900
    assert policy["premium_candles_required"] is False
    assert policy["ambiguous_snapshot_gap"] == "UNRESOLVED"
    assert policy["automatic_order_placement"] is False


def test_mcp_endpoint_is_closed_when_token_missing(monkeypatch):
    monkeypatch.delenv("MCP_ACCESS_TOKEN", raising=False)
    called = False

    async def downstream(scope, receive, send):
        nonlocal called
        called = True

    middleware = BearerTokenASGI(downstream)
    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(middleware({"type": "http", "headers": []}, None, send))
    assert called is False
    assert sent[0]["status"] == 503


def test_mcp_endpoint_rejects_wrong_token(monkeypatch):
    monkeypatch.setenv("MCP_ACCESS_TOKEN", "secret-token")
    middleware = BearerTokenASGI(lambda *_: None)
    sent = []

    async def send(message):
        sent.append(message)

    asyncio.run(middleware(
        {"type": "http", "headers": [(b"authorization", b"Bearer wrong")]},
        None,
        send,
    ))
    assert sent[0]["status"] == 401
