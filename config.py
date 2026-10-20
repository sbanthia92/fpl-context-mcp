"""
Configuration for fpl-context-mcp.

Reads from environment variables. python-dotenv is loaded first so a .env file in
the project directory (or its parent) is picked up automatically during local
development. In CI / production, the
variables are injected directly into the environment by the workflow or secrets
manager, and dotenv is a no-op.
"""

import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    # Use the nearest .env: this directory first, then its parent.
    _here = Path(__file__).resolve().parent
    for _candidate in [_here / ".env", _here.parent / ".env"]:
        if _candidate.exists():
            load_dotenv(_candidate)
            break
except ImportError:
    pass  # python-dotenv is optional; env vars must be set another way


class _Config:
    """
    Central configuration object.

    All attributes are read lazily from environment variables so that tests
    can set os.environ before importing this module. Access via the module-level
    ``cfg`` singleton.
    """

    @property
    def database_url(self) -> str:
        """
        Read-only PostgreSQL connection string (fpl_readonly user).
        Used by the MCP query_historical_stats tool.
        Format: postgresql://user:pass@host:5432/dbname
        """
        return os.getenv("DATABASE_URL", "")

    @property
    def database_etl_url(self) -> str:
        """
        Read/write PostgreSQL connection string (fpl_etl user).
        Used by ingest_match_data to write fixture and player stats rows.
        Falls back to DATABASE_URL if not set.
        """
        return os.getenv("DATABASE_ETL_URL", "") or self.database_url

    @property
    def dry_run(self) -> bool:
        """
        When True, tools return what they *would* do without any side effects, and
        ingestion jobs fetch data but skip all writes to PostgreSQL.

        Set DRY_RUN=true (or 1 / yes) to enable. Useful for verifying connectivity
        and configuration before committing to a production run.
        """
        return os.getenv("DRY_RUN", "").lower() in ("1", "true", "yes")

    @property
    def mcp_auth_token(self) -> str:
        """
        Shared secret for the HTTP transport (``--transport http``).

        When set, every request to the HTTP endpoint must carry
        ``Authorization: Bearer <token>``. Empty by default, which leaves the
        endpoint open — only acceptable when bound to localhost. Ignored by the
        stdio transport.
        """
        return os.getenv("MCP_AUTH_TOKEN", "")

    @property
    def mcp_auth_tokens(self) -> list[str]:
        """
        Accepted bearer tokens for the HTTP transport. ``MCP_AUTH_TOKEN`` may hold several
        comma-separated tokens, so a hosted deployment can hand each user their own key
        and revoke one without rotating the rest.
        """
        return [t.strip() for t in self.mcp_auth_token.split(",") if t.strip()]

    @property
    def rate_limit_per_minute(self) -> int:
        """
        HTTP transport only: max requests to /mcp per minute for each caller (bearer
        token, else client IP). 0 (default) disables limiting. One tool call from a
        client is typically 2-3 requests, so size this with headroom.
        """
        return _int_env("MCP_RATE_LIMIT_PER_MIN", 0)

    @property
    def client_ip_header(self) -> str:
        """
        HTTP transport only: header carrying the real client IP when running behind a
        proxy (e.g. ``Fly-Client-IP``, ``CF-Connecting-IP``, ``X-Forwarded-For``). Only set
        this when a trusted proxy overwrites it; otherwise callers can spoof their IP and
        dodge the rate limit. Empty (default) uses the socket peer address.
        """
        return os.getenv("MCP_CLIENT_IP_HEADER", "").strip()

    @property
    def max_concurrent_queries(self) -> int:
        """Max SQL queries running at once in this process (default 4); extras wait briefly."""
        return max(1, _int_env("MCP_MAX_CONCURRENT_QUERIES", 4))


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# Module-level singleton — import this everywhere.
cfg = _Config()
