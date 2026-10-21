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


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


# Module-level singleton — import this everywhere.
cfg = _Config()
