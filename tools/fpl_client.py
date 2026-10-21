"""
Shared client for the public FPL API, used by every tool.

Responsibilities:
  - one sync ``requests`` GET (run in a worker thread) with a User-Agent and timeout
  - an in-memory TTL cache per URL path, so several tools share one bootstrap-static
    download and heavy use costs FPL only a few calls an hour
  - stale-cache fallback when FPL is unreachable (the caller is told the data is stale)
  - small lookup helpers (team / player name resolution, table formatting)

Only the public, unauthenticated endpoints are used.
"""

import asyncio
import logging
import time

import requests

log = logging.getLogger(__name__)

BASE_URL = "https://fantasy.premierleague.com/api/"
USER_AGENT = "fpl-context-mcp/0.1"
TIMEOUT_SECONDS = 15

# Per-path cache lifetimes (seconds). Bootstrap and fixtures change slowly; a
# manager's picks change when they make transfers, so keep those fresher.
TTL_BOOTSTRAP = 300
TTL_FIXTURES = 300
TTL_ENTRY = 120
TTL_ELEMENT = 300  # a player's per-match history
TTL_LIVE_FINISHED = 3600  # a finished gameweek's stats barely change (bonus is final)
TTL_LIVE_ACTIVE = 120

_MAX_CACHE_ENTRIES = 256

POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}
POSITION_IDS = {v: k for k, v in POSITIONS.items()}

# path -> (monotonic fetch time, data)
_cache: dict[str, tuple[float, object]] = {}


class FPLNotFound(Exception):
    """FPL returned 404 for the requested resource (e.g. an unknown manager id)."""


def clear_cache() -> None:
    _cache.clear()


def _fetch(path: str):
    """Synchronous GET of an FPL API path (run in a worker thread)."""
    resp = requests.get(
        BASE_URL + path, timeout=TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}
    )
    if resp.status_code == 404:
        raise FPLNotFound(path)
    resp.raise_for_status()
    return resp.json()


async def get(path: str, ttl: int) -> tuple[object, bool]:
    """
    Return (data, is_stale) for an FPL API path.

    Serves the cache while it is younger than ``ttl``. On a fetch failure falls back
    to an older cached copy (is_stale=True) and only raises when there is none.
    ``FPLNotFound`` is never masked by the cache.
    """
    now = time.monotonic()
    cached = _cache.get(path)
    if cached is not None and now - cached[0] < ttl:
        return cached[1], False
    try:
        data = await asyncio.to_thread(_fetch, path)
    except FPLNotFound:
        raise
    except Exception:
        if cached is not None:
            log.warning("FPL fetch of %s failed; serving cached copy", path, exc_info=True)
            return cached[1], True
        raise
    if len(_cache) >= _MAX_CACHE_ENTRIES:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    _cache[path] = (time.monotonic(), data)
    return data, False


async def bootstrap() -> tuple[dict, bool]:
    return await get("bootstrap-static/", TTL_BOOTSTRAP)


async def fixtures() -> tuple[list[dict], bool]:
    return await get("fixtures/", TTL_FIXTURES)


STALE_NOTE = "Note: FPL was unreachable, so this is a cached copy and may be out of date."


def unreachable_error(exc: Exception) -> str:
    return f"Error: could not reach the FPL API ({exc})."


def format_table(columns: list[str], rows: list[dict]) -> str:
    header = " | ".join(columns)
    lines = [" | ".join(str(row[col]) for col in columns) for row in rows]
    return "\n".join([header, "-" * len(header), *lines])


def teams_by_id(boot: dict) -> dict[int, dict]:
    return {t["id"]: t for t in boot.get("teams", [])}


def resolve_team(boot: dict, query: str) -> tuple[dict | None, str | None]:
    """
    Resolve a free-text team name to a bootstrap team.

    Matches short name ('MCI'), full name ('Man City') or a unique substring of the
    name. Returns (team, None) on success or (None, error message) when there is no
    match or the query is ambiguous.
    """
    q = query.strip().lower()
    teams = boot.get("teams", [])
    exact = [t for t in teams if q in (t["short_name"].lower(), t["name"].lower())]
    if len(exact) == 1:
        return exact[0], None
    partial = [t for t in teams if q and (q in t["name"].lower() or q in t["short_name"].lower())]
    if len(partial) == 1:
        return partial[0], None
    if not partial:
        names = ", ".join(sorted(t["name"] for t in teams))
        return None, f"Error: no team matches {query!r}. Teams: {names}."
    names = ", ".join(sorted(t["name"] for t in partial))
    return None, f"Error: {query!r} is ambiguous ({names}). Use a more specific name."


def player_matches(player: dict, needles: list[str]) -> bool:
    haystack = (
        f"{player.get('first_name', '')} {player.get('second_name', '')} {player['web_name']}"
    )
    haystack = haystack.lower()
    return any(n in haystack for n in needles)


def find_player(boot: dict, query: str) -> tuple[dict | None, str | None]:
    """
    Resolve a free-text player name to one bootstrap element.

    Prefers an exact (case-insensitive) match on web name or full name, then a unique
    substring match. Returns (player, None) or (None, error message) when nothing matches
    or several players do, listing candidates so the caller can be more specific.
    """
    q = query.strip().lower()
    if not q:
        return None, "Error: give a player name."
    players = boot.get("elements", [])
    matches = [p for p in players if player_matches(p, [q])]
    if not matches:
        return None, f"Error: no player matches {query!r}."
    if len(matches) > 1:
        exact = [
            p
            for p in matches
            if q in (p["web_name"].lower(), f"{p['first_name']} {p['second_name']}".lower())
        ]
        if len(exact) == 1:
            return exact[0], None
        teams = teams_by_id(boot)
        matches.sort(key=lambda p: p.get("total_points", 0), reverse=True)
        shown = ", ".join(
            f"{p['web_name']} ({teams.get(p['team'], {}).get('short_name', '?')}, "
            f"{POSITIONS.get(p['element_type'], '?')})"
            for p in matches[:8]
        )
        return None, f"Error: {query!r} matches several players: {shown}. Be more specific."
    return matches[0], None


def current_gameweek(boot: dict) -> dict | None:
    """
    The gameweek still to be settled: the one in progress, else the next to play.

    A current gameweek that has already finished is skipped in favour of the next one.
    Returns None when the season is over.
    """
    events = boot.get("events", [])
    current = next((e for e in events if e.get("is_current")), None)
    if current and not current.get("finished"):
        return current
    return next((e for e in events if e.get("is_next")), current if current else None)


def to_int(value, name: str) -> tuple[int | None, str | None]:
    """Coerce a tool argument to int, returning (value, error message)."""
    try:
        return int(value), None
    except (TypeError, ValueError):
        return None, f"Error: {name} must be a whole number, got {value!r}."
