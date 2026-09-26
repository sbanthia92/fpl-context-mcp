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
    def pinecone_api_key(self) -> str:
        """Pinecone API key. Required for RAG tools and press ingestion."""
        return os.getenv("PINECONE_API_KEY", "")

    @property
    def pinecone_index_name(self) -> str:
        """Pinecone index name. Defaults to 'fpl-context'; override with PINECONE_INDEX_NAME."""
        return os.getenv("PINECONE_INDEX_NAME") or "fpl-context"

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
    def guardian_api_key(self) -> str:
        """
        The Guardian open platform API key.
        Register for free at https://open-platform.theguardian.com/access/.
        Empty by default: the Guardian API rejects the old public 'test' key (HTTP 401),
        so without a registered key the Guardian source is skipped and only BBC Sport
        articles are ingested.
        """
        return os.getenv("GUARDIAN_API_KEY", "")

    @property
    def dry_run(self) -> bool:
        """
        When True, tools return what they *would* do without any side effects, and
        ingestion jobs fetch data but skip all writes to Pinecone and PostgreSQL.

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


# Module-level singleton — import this everywhere.
cfg = _Config()
