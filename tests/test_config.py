"""Tests for config.py — verifies env-var reading and fallback behaviour."""

import pytest

from config import _Config


def test_database_etl_url_falls_back_to_database_url(monkeypatch):
    """DATABASE_ETL_URL falls back to DATABASE_URL when unset."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://ro:x@localhost/db")
    monkeypatch.delenv("DATABASE_ETL_URL", raising=False)
    c = _Config()
    assert c.database_etl_url == "postgresql://ro:x@localhost/db"


def test_database_etl_url_takes_precedence(monkeypatch):
    """DATABASE_ETL_URL is used when explicitly set."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://ro:x@localhost/db")
    monkeypatch.setenv("DATABASE_ETL_URL", "postgresql://etl:y@localhost/db")
    c = _Config()
    assert c.database_etl_url == "postgresql://etl:y@localhost/db"


def test_dry_run_defaults_to_false(monkeypatch):
    """DRY_RUN is False when the env var is unset."""
    monkeypatch.delenv("DRY_RUN", raising=False)
    assert _Config().dry_run is False


@pytest.mark.parametrize("value", ["true", "True", "TRUE", "1", "yes", "YES"])
def test_dry_run_truthy_values(monkeypatch, value):
    """DRY_RUN accepts common truthy string values."""
    monkeypatch.setenv("DRY_RUN", value)
    assert _Config().dry_run is True


def test_dry_run_false_string(monkeypatch):
    """DRY_RUN=false is not truthy."""
    monkeypatch.setenv("DRY_RUN", "false")
    assert _Config().dry_run is False


def test_mcp_auth_token_default_empty(monkeypatch):
    """MCP_AUTH_TOKEN defaults to empty (HTTP endpoint unauthenticated)."""
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    assert _Config().mcp_auth_token == ""


def test_mcp_auth_token_reads_env(monkeypatch):
    """MCP_AUTH_TOKEN is read from the environment."""
    monkeypatch.setenv("MCP_AUTH_TOKEN", "s3cret")
    assert _Config().mcp_auth_token == "s3cret"
