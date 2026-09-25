"""Tests for the streamable HTTP transport and CLI argument parsing in server.py."""

from starlette.testclient import TestClient

import server

MCP_HEADERS = {
    "Accept": "application/json, text/event-stream",
    "Content-Type": "application/json",
}

INITIALIZE = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}


def test_health_is_open_even_with_token():
    """/health answers without auth so hosting platforms can probe it."""
    with TestClient(server.build_http_app("secret")) as client:
        r = client.get("/health")
    assert r.status_code == 200
    assert r.text == "ok"


def test_mcp_rejects_missing_token():
    """/mcp returns 401 when MCP_AUTH_TOKEN is set and no bearer token is sent."""
    with TestClient(server.build_http_app("secret")) as client:
        r = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert r.status_code == 401


def test_mcp_rejects_wrong_token():
    """/mcp returns 401 for a bearer token that doesn't match."""
    headers = {**MCP_HEADERS, "Authorization": "Bearer nope"}
    with TestClient(server.build_http_app("secret")) as client:
        r = client.post("/mcp", json=INITIALIZE, headers=headers)
    assert r.status_code == 401


def test_mcp_initialize_with_token():
    """A correctly authenticated initialize request reaches the MCP server."""
    headers = {**MCP_HEADERS, "Authorization": "Bearer secret"}
    with TestClient(server.build_http_app("secret")) as client:
        r = client.post("/mcp", json=INITIALIZE, headers=headers)
    assert r.status_code == 200
    assert "fpl-context-mcp" in r.text


def test_mcp_open_without_token():
    """With no token configured, /mcp is served without auth (localhost use)."""
    with TestClient(server.build_http_app("")) as client:
        r = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS)
    assert r.status_code == 200
    assert "fpl-context-mcp" in r.text


def test_mcp_path_does_not_redirect():
    """/mcp is served directly — no 307 to /mcp/, which some clients don't follow."""
    with TestClient(server.build_http_app("")) as client:
        r = client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS, follow_redirects=False)
    assert r.status_code == 200


def test_parse_args_defaults(monkeypatch):
    """Defaults to stdio on 127.0.0.1:8000."""
    for var in ("MCP_TRANSPORT", "MCP_HOST", "MCP_PORT", "PORT"):
        monkeypatch.delenv(var, raising=False)
    args = server._parse_args([])
    assert (args.transport, args.host, args.port, args.check) == ("stdio", "127.0.0.1", 8000, False)


def test_parse_args_env_fallbacks(monkeypatch):
    """MCP_TRANSPORT / MCP_HOST / PORT set the defaults (for container hosting)."""
    monkeypatch.setenv("MCP_TRANSPORT", "http")
    monkeypatch.setenv("MCP_HOST", "0.0.0.0")
    monkeypatch.delenv("MCP_PORT", raising=False)
    monkeypatch.setenv("PORT", "9000")
    args = server._parse_args([])
    assert (args.transport, args.host, args.port) == ("http", "0.0.0.0", 9000)


def test_parse_args_flags_override_env(monkeypatch):
    """Command-line flags win over environment variables."""
    monkeypatch.setenv("MCP_PORT", "9000")
    args = server._parse_args(["--transport", "http", "--port", "1234", "--check"])
    assert (args.transport, args.port, args.check) == ("http", 1234, True)
