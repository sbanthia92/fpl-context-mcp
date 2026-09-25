"""
fpl-context-mcp — MCP server entry point.

Exposes two tools over MCP, via stdio (default) or streamable HTTP:

  query_historical_stats   — read-only SQL against the FPL PostgreSQL database
  query_press_conferences  — semantic search over the Pinecone 'press' namespace

Run locally:
    cd fpl-context-mcp
    python server.py                      # stdio
    python server.py --transport http     # http://127.0.0.1:8000/mcp

Register in Claude Desktop (claude_desktop_config.json):
    {
      "mcpServers": {
        "fpl-context": {
          "command": "python",
          "args": ["/absolute/path/to/fpl-context-mcp/server.py"]
        }
      }
    }
"""

import asyncio
import logging
import sys

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from config import cfg
from tools.query_historical_stats import (
    SCHEMA_DESCRIPTION,
    query_historical_stats,
)
from tools.query_press_conferences import query_press_conferences

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)


def check_config() -> None:
    """
    Validate configuration and test connectivity for each configured component.

    Prints a human-readable summary of what is and isn't set up correctly.
    Exits with code 1 if any required component fails its connectivity check.
    """
    import asyncio

    ok = True
    lines = ["\n=== fpl-context-mcp configuration check ===\n"]

    # --- Pinecone ---
    if not cfg.pinecone_api_key:
        lines.append("❌ PINECONE_API_KEY  not set (required for query_press_conferences)")
        ok = False
    else:
        try:
            from pinecone import Pinecone as _PC

            pc = _PC(api_key=cfg.pinecone_api_key)
            pc.Index(cfg.pinecone_index_name).describe_index_stats()
            lines.append(f"✅ Pinecone          connected (index: {cfg.pinecone_index_name!r})")
        except Exception as exc:
            lines.append(f"❌ Pinecone          connection failed: {exc}")
            ok = False

    # --- PostgreSQL (read-only) ---
    if not cfg.database_url:
        lines.append("⚠️  DATABASE_URL      not set (query_historical_stats will be unavailable)")
    else:
        try:
            import asyncpg

            async def _ping() -> None:
                conn = await asyncpg.connect(cfg.database_url)
                await conn.fetchval("SELECT 1")
                await conn.close()

            asyncio.run(_ping())
            lines.append(f"✅ PostgreSQL (RO)   connected ({cfg.database_url.split('@')[-1]})")
        except Exception as exc:
            lines.append(f"❌ PostgreSQL (RO)   connection failed: {exc}")
            ok = False

    # --- PostgreSQL (ETL / read-write) ---
    if not cfg.database_etl_url:
        lines.append("⚠️  DATABASE_ETL_URL  not set (ingest_match_data will be unavailable)")
    else:
        try:
            import psycopg2

            conn = psycopg2.connect(cfg.database_etl_url, connect_timeout=5)
            conn.close()
            url_display = cfg.database_etl_url.split("@")[-1]
            lines.append(f"✅ PostgreSQL (ETL)  connected ({url_display})")
        except Exception as exc:
            lines.append(f"❌ PostgreSQL (ETL)  connection failed: {exc}")
            ok = False

    # --- Guardian API (optional) ---
    if not cfg.guardian_api_key:
        lines.append(
            "⚠️  GUARDIAN_API_KEY  not set (Guardian articles will be skipped, BBC Sport only — "
            "register a free key at open-platform.theguardian.com/access)"
        )
    else:
        lines.append("✅ Guardian API      registered key configured")

    # --- Dry-run flag ---
    if cfg.dry_run:
        lines.append("\n🔁 DRY_RUN=true — no writes will be made")

    summary = "✅ All required components OK" if ok else "❌ One or more components failed"
    lines.append(f"\n{summary}\n")
    print("\n".join(lines))
    sys.exit(0 if ok else 1)


def _package_version() -> str | None:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("fpl-context-mcp")
    except PackageNotFoundError:  # running from a source checkout without install
        return None


server = Server("fpl-context-mcp", version=_package_version())


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """
    Advertise the two MCP tools this server exposes.

    Called by the MCP client (e.g. Claude Desktop) during initialisation to
    discover what capabilities are available.

    Returns:
        List of Tool descriptors with names, descriptions, and JSON Schema for
        their input parameters.
    """
    return [
        types.Tool(
            name="query_historical_stats",
            description=(
                "Execute a read-only SQL SELECT against the FPL stats "
                "database. Use this to answer questions about player stats, fixtures, "
                "team strength, or gameweek history for the seasons in the database.\n\n"
                + SCHEMA_DESCRIPTION
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": (
                            "A SQL SELECT statement. Only SELECT is allowed — "
                            "mutations will be rejected. LIMIT is injected automatically "
                            "if omitted (max 100 rows)."
                        ),
                    }
                },
                "required": ["sql"],
            },
        ),
        types.Tool(
            name="query_press_conferences",
            description=(
                "Semantic search over Premier League press conference summaries, match "
                "reports, and player injury/availability updates ingested from BBC Sport "
                "and The Guardian. Use this to find recent quotes, injury news, or "
                "manager/team news."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Natural-language question or topic to search for.",
                    },
                    "top_k": {
                        "type": "integer",
                        "description": "Number of documents to retrieve. Default 5.",
                        "default": 5,
                    },
                    "recency_weight": {
                        "type": "number",
                        "description": (
                            "How strongly to boost recent articles in ranking. "
                            "0.0 = pure semantic similarity, 1.0 = heavy recency bias. "
                            "Default 0.3."
                        ),
                        "default": 0.3,
                    },
                },
                "required": ["query"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(
    name: str,
    arguments: dict,
) -> list[types.TextContent]:
    """
    Dispatch an incoming tool call to the appropriate implementation.

    Args:
        name:      Tool name as registered in list_tools().
        arguments: Dict of arguments matching the tool's inputSchema.

    Returns:
        List containing a single TextContent with the tool's output.

    Raises:
        ValueError: If an unknown tool name is requested.
    """
    log.info("tool call: %s args=%r", name, arguments)

    if name == "query_historical_stats":
        sql = arguments.get("sql", "")
        result = await query_historical_stats(sql)

    elif name == "query_press_conferences":
        query = arguments.get("query", "")
        top_k = int(arguments.get("top_k", 5))
        recency_weight = float(arguments.get("recency_weight", 0.3))
        result = await query_press_conferences(
            query=query,
            top_k=top_k,
            recency_weight=recency_weight,
        )

    else:
        raise ValueError(f"Unknown tool: {name!r}")

    return [types.TextContent(type="text", text=result)]


def _warn_if_dry_run() -> None:
    if cfg.dry_run:
        log.warning(
            "DRY RUN MODE — tools will return what they would do without side effects. "
            "Set DRY_RUN=false to disable."
        )


async def _serve() -> None:
    """Wire the MCP server to stdio and run until the client disconnects."""
    _warn_if_dry_run()
    async with stdio_server() as (read_stream, write_stream):
        log.info("fpl-context-mcp server started (stdio transport)")
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def build_http_app(auth_token: str = ""):
    """
    Build the ASGI app for the streamable HTTP transport.

    Routes:
        /mcp     — MCP streamable HTTP endpoint (stateless, so any replica can
                   serve any request)
        /health  — unauthenticated liveness probe for hosting platforms

    Args:
        auth_token: When non-empty, /mcp requires ``Authorization: Bearer <token>``.
    """
    import contextlib
    import hmac

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route

    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)
    expected = f"Bearer {auth_token}".encode()

    class _MCPEndpoint:
        # A class instance (not a function) so Starlette's Route passes the raw
        # ASGI scope through instead of wrapping it as a request handler.
        async def __call__(self, scope, receive, send) -> None:
            if auth_token:
                headers = dict(scope.get("headers") or [])
                given = headers.get(b"authorization", b"")
                if not hmac.compare_digest(given, expected):
                    response = JSONResponse({"error": "unauthorized"}, status_code=401)
                    await response(scope, receive, send)
                    return
            await session_manager.handle_request(scope, receive, send)

    async def health(request):
        return PlainTextResponse("ok")

    @contextlib.asynccontextmanager
    async def lifespan(app):
        async with session_manager.run():
            yield

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/mcp", _MCPEndpoint(), methods=["GET", "POST", "DELETE"]),
        ],
        lifespan=lifespan,
    )


def _serve_http(host: str, port: int) -> None:
    """Run the MCP server over streamable HTTP at http://host:port/mcp."""
    import uvicorn

    _warn_if_dry_run()
    token = cfg.mcp_auth_token
    if not token and host not in _LOOPBACK_HOSTS:
        log.warning(
            "MCP_AUTH_TOKEN is not set and the server is bound to %s — anyone who can "
            "reach this address can query your database and Pinecone index. Set "
            "MCP_AUTH_TOKEN, or put the server behind an authenticating proxy.",
            host,
        )
    log.info("fpl-context-mcp server started (http transport) on http://%s:%d/mcp", host, port)
    uvicorn.run(build_http_app(token), host=host, port=port, log_level="info")


def _parse_args(argv: list[str]):
    import argparse
    import os

    parser = argparse.ArgumentParser(
        prog="fpl-context-mcp",
        description="FPL stats and press-coverage MCP server.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate configuration and test connectivity, then exit",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default=os.getenv("MCP_TRANSPORT", "stdio"),
        help="stdio (default; launched by a local MCP client) or http (streamable "
        "HTTP at /mcp, for remote clients such as ChatGPT connectors)",
    )
    parser.add_argument(
        "--host",
        default=os.getenv("MCP_HOST", "127.0.0.1"),
        help="HTTP bind address (default 127.0.0.1; use 0.0.0.0 when hosting)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.getenv("MCP_PORT") or os.getenv("PORT") or 8000),
        help="HTTP port (default 8000, or $MCP_PORT / $PORT)",
    )
    return parser.parse_args(argv)


def main() -> None:
    """Entry point — run the MCP server over stdio (default) or HTTP.

    Pass --check to validate configuration and test connectivity without
    starting the MCP server.
    """
    args = _parse_args(sys.argv[1:])
    if args.check:
        check_config()
        return
    if args.transport == "http":
        _serve_http(args.host, args.port)
        return
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
