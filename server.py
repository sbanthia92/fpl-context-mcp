"""
fpl-context-mcp — MCP server entry point.

Exposes these tools over MCP, via stdio (default) or streamable HTTP:

  query_historical_stats   — read-only SQL against the FPL PostgreSQL database
                             (stats, fixtures, and player injury/availability);
                             listed only when DATABASE_URL is set
  get_live_availability    — current injury/suspension flags fetched live from FPL
  get_current_gameweek     — live current/next gameweek and deadline
  get_fixtures             — live fixtures with difficulty ratings (FDR)
  search_players           — live player search and ranking
  get_manager_squad        — a manager's squad and chips by FPL team ID
  get_fixture_run          — upcoming fixture difficulty, one team or a ranked ticker
  get_xpts                 — estimated expected points for a gameweek

All tools except query_historical_stats call the public FPL API directly and need no
database, so ``uvx fpl-context-mcp`` works with zero setup.

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
from collections.abc import Awaitable, Callable

import mcp.types as types
from mcp.server import Server
from mcp.server.stdio import stdio_server

from config import cfg
from tools.get_current_gameweek import get_current_gameweek
from tools.get_fixture_run import get_fixture_run
from tools.get_fixtures import get_fixtures
from tools.get_live_availability import get_live_availability
from tools.get_manager_squad import get_manager_squad
from tools.get_xpts import get_xpts
from tools.query_historical_stats import (
    SCHEMA_DESCRIPTION,
    query_historical_stats,
)
from tools.search_players import SORT_FIELDS, search_players

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


def _ro(title: str) -> types.ToolAnnotations:
    """Annotations for a tool that only reads: clients can skip approval prompts."""
    return types.ToolAnnotations(
        title=title, readOnlyHint=True, destructiveHint=False, idempotentHint=True
    )


_TEAM_PARAM = {
    "type": "string",
    "description": "Team name or short name, e.g. 'Arsenal', 'Man City' or 'MCI'.",
}

# name -> (descriptor, handler(arguments) -> str). Order is the order clients see.
_TOOLS: dict[str, tuple[types.Tool, Callable[[dict], Awaitable[str]]]] = {}


def _register(tool: types.Tool, handler: Callable[[dict], Awaitable[str]]) -> None:
    _TOOLS[tool.name] = (tool, handler)


_register(
    types.Tool(
        name="query_historical_stats",
        description=(
            "Execute a read-only SQL SELECT against the FPL stats "
            "database. Use this to answer questions about player stats, fixtures, "
            "team strength, gameweek history, and player injury/availability status "
            "(FPL's status, chance of playing and news note) for the seasons in the "
            "database. Availability here is as of the last data refresh; call "
            "get_live_availability for the current flags.\n\n" + SCHEMA_DESCRIPTION
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
        annotations=_ro("Query FPL stats database"),
    ),
    lambda a: query_historical_stats(a.get("sql", "")),
)

_register(
    types.Tool(
        name="get_live_availability",
        description=(
            "Current FPL injury, suspension and availability flags, fetched live from "
            "the FPL API (no database needed). Call this before recommending, picking "
            "or captaining any player, so a newly flagged or suspended player isn't "
            "missed. With no arguments it lists every player who currently has a flag, "
            "newest news first. Pass player names to check specific players (returned "
            "even when fully available), and/or statuses to filter.\n\n"
            "Status codes: a available, d doubtful, i injured, s suspended, "
            "u unavailable (left club / on loan), n not in squad. "
            "chance_of_playing is FPL's estimate for the next round."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "players": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Player names to look up, e.g. ['Saka', 'Haaland']. "
                        "Case-insensitive; matches first, second or web name."
                    ),
                },
                "statuses": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["a", "d", "i", "s", "u", "n"]},
                    "description": "Only return players with these FPL status codes.",
                },
            },
        },
        annotations=_ro("Live injury and suspension flags"),
    ),
    lambda a: get_live_availability(players=a.get("players"), statuses=a.get("statuses")),
)

_register(
    types.Tool(
        name="get_current_gameweek",
        description=(
            "The current and next FPL gameweek and the transfer deadline, live from the "
            "FPL API (no database needed). Use it to anchor 'this week' / 'next week' "
            "questions and to say how long a manager has left to make transfers."
        ),
        inputSchema={"type": "object", "properties": {}},
        annotations=_ro("Current gameweek and deadline"),
    ),
    lambda a: get_current_gameweek(),
)

_register(
    types.Tool(
        name="get_fixtures",
        description=(
            "FPL fixtures with difficulty ratings (FDR, 1 easiest to 5 hardest), live "
            "from the FPL API (no database needed). With no arguments lists the next 5 "
            "gameweeks for every team. Pass team to see one team's run from its own point "
            "of view, gameweek for a single round, or gameweeks_ahead for a longer window."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "team": _TEAM_PARAM,
                "gameweek": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 38,
                    "description": "A single gameweek. Overrides gameweeks_ahead.",
                },
                "gameweeks_ahead": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 38,
                    "description": "Gameweeks to list from the next one to play (default 5).",
                },
            },
        },
        annotations=_ro("Fixtures with difficulty"),
    ),
    lambda a: get_fixtures(
        team=a.get("team"),
        gameweek=a.get("gameweek"),
        gameweeks_ahead=a.get("gameweeks_ahead"),
    ),
)

_register(
    types.Tool(
        name="search_players",
        description=(
            "Find and rank current-season FPL players, live from the FPL API (no database "
            "needed): by name, team, position and maximum price, sorted by a chosen stat. "
            "Returns price, total points, form, ownership, FPL's expected points for the "
            "next gameweek (ep_next) and availability status. Use it for 'best midfielders "
            "under £8m', 'how is Palmer doing', or to find a replacement."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Part of a first, second or web name, e.g. 'saka'.",
                },
                "team": _TEAM_PARAM,
                "position": {"type": "string", "enum": ["GKP", "DEF", "MID", "FWD"]},
                "max_price": {"type": "number", "description": "Maximum price in £m, e.g. 7.5."},
                "sort_by": {
                    "type": "string",
                    "enum": list(SORT_FIELDS),
                    "description": "Stat to rank by, highest first (default total_points).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Rows to return (default 15).",
                },
            },
        },
        annotations=_ro("Search and rank players"),
    ),
    lambda a: search_players(
        name=a.get("name"),
        team=a.get("team"),
        position=a.get("position"),
        max_price=a.get("max_price"),
        sort_by=a.get("sort_by") or "total_points",
        limit=a.get("limit"),
    ),
)

_register(
    types.Tool(
        name="get_manager_squad",
        description=(
            "A manager's FPL squad for a gameweek, with captain, bench, bank, squad value, "
            "transfers and the chips they have used, looked up by their FPL team ID "
            "(live from FPL's public API, no login or database needed). The team ID is the "
            "number in the URL of the manager's FPL Points page. Use it as the starting "
            "point for transfer, captain and chip advice on their actual team."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "team_id": {"type": "integer", "minimum": 1, "description": "FPL team ID."},
                "gameweek": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 38,
                    "description": "Gameweek to show (default: the manager's current one).",
                },
            },
            "required": ["team_id"],
        },
        annotations=_ro("Manager squad and chips"),
    ),
    lambda a: get_manager_squad(team_id=a.get("team_id"), gameweek=a.get("gameweek")),
)


_register(
    types.Tool(
        name="get_fixture_run",
        description=(
            "How easy or hard a team's next few fixtures are, live from the FPL API (no "
            "database needed): average FDR (1 easiest, 5 hardest), home/away split, and "
            "blank or double gameweeks. Pass team for its fixture-by-fixture run; omit it "
            "to rank all 20 teams, easiest run first. Prefer this over get_fixtures when "
            "judging transfers, captaincy or chip timing."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "team": _TEAM_PARAM,
                "gameweeks": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 38,
                    "description": "Window length from the next gameweek (default 5).",
                },
            },
        },
        annotations=_ro("Fixture run difficulty"),
    ),
    lambda a: get_fixture_run(team=a.get("team"), gameweeks=a.get("gameweeks")),
)

_register(
    types.Tool(
        name="get_xpts",
        description=(
            "Rank players by estimated expected points (xPts) for a gameweek, live from "
            "the FPL API (no database needed). A transparent baseline built from season "
            "xG/xA/clean-sheet/save/bonus rates, expected minutes, availability and fixture "
            "difficulty, shown beside FPL's own ep_next. Filter by position, team and max "
            "price. Good for captain shortlists and finding transfer targets; it is "
            "noisy early in the season and ignores rotation, so cross-check availability "
            "with get_live_availability."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "position": {"type": "string", "enum": ["GKP", "DEF", "MID", "FWD"]},
                "team": _TEAM_PARAM,
                "max_price": {"type": "number", "description": "Maximum price in £m, e.g. 7.5."},
                "gameweek": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 38,
                    "description": "Gameweek to project (default: the next one to play).",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 50,
                    "description": "Rows to return (default 15).",
                },
            },
        },
        annotations=_ro("Expected points ranking"),
    ),
    lambda a: get_xpts(
        position=a.get("position"),
        team=a.get("team"),
        max_price=a.get("max_price"),
        gameweek=a.get("gameweek"),
        limit=a.get("limit"),
    ),
)


def _database_configured() -> bool:
    return bool(cfg.database_url)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """
    Advertise the MCP tools this server exposes.

    Called by the MCP client (e.g. Claude Desktop) during initialisation to
    discover what capabilities are available. ``query_historical_stats`` is only
    listed when DATABASE_URL is set, so a database-free install never shows the
    model a tool that can only fail.

    Returns:
        List of Tool descriptors with names, descriptions, and JSON Schema for
        their input parameters.
    """
    return [
        tool
        for name, (tool, _) in _TOOLS.items()
        if name != "query_historical_stats" or _database_configured()
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

    entry = _TOOLS.get(name)
    if entry is None:
        raise ValueError(f"Unknown tool: {name!r}")
    result = await entry[1](arguments or {})
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
            "reach this address can query your database. Set "
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
        description="FPL stats and player-availability MCP server.",
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
