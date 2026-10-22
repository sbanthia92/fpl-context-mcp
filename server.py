"""
fpl-context-mcp — MCP server entry point.

Exposes these tools over MCP, via stdio (default) or streamable HTTP. All of them call the
public FPL API directly, so ``uvx fpl-context-mcp`` works with zero setup and no database:

  get_current_gameweek     — current/next gameweek and deadline
  get_fixtures             — fixtures with difficulty ratings (FDR)
  get_fixture_run          — upcoming fixture difficulty, one team or a ranked ticker
  search_players           — player search and ranking
  get_player_history       — a player's matches this season (vs an opponent) and past totals
  get_gameweek_stats       — every player's stats for one gameweek
  get_xpts                 — estimated expected points for a gameweek
  get_manager_squad        — a manager's squad and chips by FPL team ID
  get_live_availability    — current injury/suspension flags

Also exposes MCP prompts (captain_pick, transfer_plan, chip_timing: slash commands in
many clients) and a resource (fpl://glossary).

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
from mcp.server.lowlevel.helper_types import ReadResourceContents
from mcp.server.stdio import stdio_server

from config import cfg
from tools import fpl_client
from tools.get_current_gameweek import get_current_gameweek
from tools.get_fixture_run import get_fixture_run
from tools.get_fixtures import get_fixtures
from tools.get_gameweek_stats import SORT_FIELDS as GW_SORT_FIELDS
from tools.get_gameweek_stats import get_gameweek_stats
from tools.get_live_availability import get_live_availability
from tools.get_manager_squad import get_manager_squad
from tools.get_player_history import get_player_history
from tools.get_xpts import get_xpts
from tools.prompts import get_prompt as render_prompt
from tools.prompts import list_prompts as _list_prompts
from tools.ratelimit import RateLimiter
from tools.resources import list_resources as _list_resources
from tools.resources import read_resource as _read_resource
from tools.search_players import SORT_FIELDS, search_players

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)


def check_config() -> None:
    """
    Check that the FPL API is reachable and print a short summary.

    The server needs no configuration; this only confirms outbound access to
    fantasy.premierleague.com. Exits with code 1 if the check fails.
    """
    import requests

    lines = ["\n=== fpl-context-mcp check ===\n"]
    ok = True
    try:
        resp = requests.get(
            fpl_client.BASE_URL + "bootstrap-static/",
            timeout=fpl_client.TIMEOUT_SECONDS,
            headers={"User-Agent": fpl_client.USER_AGENT},
        )
        resp.raise_for_status()
        data = resp.json()
        lines.append(
            f"✅ FPL API reachable ({len(data.get('elements', []))} players, "
            f"{len(data.get('teams', []))} teams)"
        )
    except Exception as exc:
        lines.append(f"❌ FPL API not reachable: {exc}")
        ok = False
    if cfg.mcp_auth_tokens:
        lines.append(f"🔑 HTTP auth: {len(cfg.mcp_auth_tokens)} key(s) configured")
    lines.append(f"\n{'✅ OK' if ok else '❌ Failed'}\n")
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
        name="get_live_availability",
        description=(
            "Current FPL injury, suspension and availability flags, fetched live from "
            "the FPL API. Call this before recommending, picking "
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
            "FPL API. Use it to anchor 'this week' / 'next week' "
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
            "from the FPL API. With no arguments lists the next 5 "
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
            "Find and rank current-season FPL players, live from the FPL API: by name, "
            "team, position and maximum price, sorted by a chosen stat. "
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
            "(live from FPL's public API, no login needed). The team ID is the "
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
            "How easy or hard a team's next few fixtures are, live from the FPL API: "
            "average FDR (1 easiest, 5 hardest), home/away split, and "
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
            "the FPL API. A transparent baseline built from season "
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


_register(
    types.Tool(
        name="get_player_history",
        description=(
            "A player's match-by-match record this season (minutes, goals, assists, clean "
            "sheets, bonus, xG/xA, points), optionally only against one opponent or the last "
            "N matches, plus their season totals for past seasons. Live from the FPL API. "
            "Use it for 'how has Saka done against Spurs this season' or "
            "'is his form real'. FPL provides match-level data for the current season only; "
            "past seasons are totals, so opponent splits for earlier seasons do not exist."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "player": {
                    "type": "string",
                    "description": "Player name, e.g. 'Saka' or 'Bukayo Saka'.",
                },
                "opponent": {**_TEAM_PARAM, "description": "Only matches against this team."},
                "last_n": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 40,
                    "description": "Only the most recent N matches.",
                },
            },
            "required": ["player"],
        },
        annotations=_ro("Player match history"),
    ),
    lambda a: get_player_history(
        player=a.get("player", ""), opponent=a.get("opponent"), last_n=a.get("last_n")
    ),
)

_register(
    types.Tool(
        name="get_gameweek_stats",
        description=(
            "Every player's stats for one gameweek, ranked (default: the latest gameweek with "
            "results, sorted by points). Live from the FPL API. Use it "
            "for 'top scorers last week', 'which defenders kept clean sheets in GW9' or "
            "'who got the most bonus'. Filter by position and team."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "gameweek": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 38,
                    "description": "Gameweek (default: the latest with results).",
                },
                "position": {"type": "string", "enum": ["GKP", "DEF", "MID", "FWD"]},
                "team": _TEAM_PARAM,
                "sort_by": {
                    "type": "string",
                    "enum": list(GW_SORT_FIELDS),
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
        annotations=_ro("Gameweek stats ranking"),
    ),
    lambda a: get_gameweek_stats(
        gameweek=a.get("gameweek"),
        position=a.get("position"),
        team=a.get("team"),
        sort_by=a.get("sort_by") or "total_points",
        limit=a.get("limit"),
    ),
)


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """
    Advertise the MCP tools this server exposes.

    Called by the MCP client (e.g. Claude Desktop) during initialisation to
    discover what capabilities are available.

    Returns:
        List of Tool descriptors with names, descriptions, and JSON Schema for
        their input parameters.
    """
    return [tool for tool, _ in _TOOLS.values()]


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


@server.list_prompts()
async def list_prompts() -> list[types.Prompt]:
    """Ready-made workflows (captain pick, transfer plan, chip timing) shown as slash commands."""
    return _list_prompts()


@server.get_prompt()
async def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    return render_prompt(name, arguments)


@server.list_resources()
async def list_resources() -> list[types.Resource]:
    """Reference material: an FPL glossary and tool guide."""
    return _list_resources()


@server.read_resource()
async def read_resource(uri) -> list[ReadResourceContents]:
    return [ReadResourceContents(content=_read_resource(str(uri)), mime_type="text/plain")]


async def _serve() -> None:
    """Wire the MCP server to stdio and run until the client disconnects."""
    async with stdio_server() as (read_stream, write_stream):
        log.info("fpl-context-mcp server started (stdio transport)")
        await server.run(
            read_stream,
            write_stream,
            server.create_initialization_options(),
        )


_LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}


def build_http_app(auth_token: str = "", rate_limit: int = 0, client_ip_header: str = ""):
    """
    Build the ASGI app for the streamable HTTP transport.

    Routes:
        /mcp     — MCP streamable HTTP endpoint (stateless, so any replica can
                   serve any request)
        /health  — unauthenticated liveness probe for hosting platforms

    Args:
        auth_token:       When non-empty, /mcp requires ``Authorization: Bearer <token>``.
                          May hold several comma-separated tokens.
        rate_limit:       Max /mcp requests per minute per caller (0 = unlimited).
                          Callers are keyed by bearer token, else client IP.
        client_ip_header: Header carrying the real client IP behind a trusted proxy.
    """
    import contextlib
    import hmac

    from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse, PlainTextResponse
    from starlette.routing import Route

    session_manager = StreamableHTTPSessionManager(app=server, stateless=True)
    expected = [f"Bearer {t.strip()}".encode() for t in auth_token.split(",") if t.strip()]
    limiter = RateLimiter(rate_limit)
    ip_header = client_ip_header.lower().encode()

    def _caller_key(scope, headers) -> str:
        given = headers.get(b"authorization", b"")
        if expected and given:
            return "token:" + given.decode("latin-1")
        if ip_header and headers.get(ip_header):
            return "ip:" + headers[ip_header].decode("latin-1").split(",")[0].strip()
        client = scope.get("client")
        return "ip:" + (client[0] if client else "unknown")

    class _MCPEndpoint:
        # A class instance (not a function) so Starlette's Route passes the raw
        # ASGI scope through instead of wrapping it as a request handler.
        async def __call__(self, scope, receive, send) -> None:
            headers = dict(scope.get("headers") or [])
            if expected:
                given = headers.get(b"authorization", b"")
                # Check every token (no early exit) so timing doesn't reveal which matched.
                matched = [hmac.compare_digest(given, e) for e in expected]
                if not any(matched):
                    response = JSONResponse({"error": "unauthorized"}, status_code=401)
                    await response(scope, receive, send)
                    return
            retry_after = limiter.check(_caller_key(scope, headers))
            if retry_after:
                response = JSONResponse(
                    {"error": "rate limit exceeded"},
                    status_code=429,
                    headers={"Retry-After": str(int(retry_after) + 1)},
                )
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

    token = cfg.mcp_auth_token
    rate_limit = cfg.rate_limit_per_minute
    if not token and host not in _LOOPBACK_HOSTS:
        log.warning(
            "MCP_AUTH_TOKEN is not set and the server is bound to %s — anyone who can "
            "reach this address can use it (and spend your FPL API quota). Set "
            "MCP_AUTH_TOKEN, or put the server behind an authenticating proxy.",
            host,
        )
    if host not in _LOOPBACK_HOSTS and not rate_limit:
        log.warning(
            "MCP_RATE_LIMIT_PER_MIN is not set: a public endpoint has no per-caller request "
            "limit. Set it (e.g. 120) before exposing the server."
        )
    log.info("fpl-context-mcp server started (http transport) on http://%s:%d/mcp", host, port)
    app = build_http_app(token, rate_limit, cfg.client_ip_header)
    uvicorn.run(app, host=host, port=port, log_level="info")


def _parse_args(argv: list[str]):
    import argparse
    import os

    parser = argparse.ArgumentParser(
        prog="fpl-context-mcp",
        description="Live Fantasy Premier League data for AI agents.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="check that the FPL API is reachable, then exit",
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
