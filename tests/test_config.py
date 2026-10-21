"""Tests for config.py — verifies env-var reading and fallback behaviour."""

from config import _Config


def test_mcp_auth_token_default_empty(monkeypatch):
    """MCP_AUTH_TOKEN defaults to empty (HTTP endpoint unauthenticated)."""
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    assert _Config().mcp_auth_token == ""


def test_mcp_auth_token_reads_env(monkeypatch):
    """MCP_AUTH_TOKEN is read from the environment."""
    monkeypatch.setenv("MCP_AUTH_TOKEN", "s3cret")
    assert _Config().mcp_auth_token == "s3cret"
