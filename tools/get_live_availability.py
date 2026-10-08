"""
MCP tool: get_live_availability

Fetches every player's current FPL availability (status, chance of playing and the
news note) straight from the FPL API's bootstrap-static endpoint, so injury and
suspension flags are as fresh as FPL's own site rather than as fresh as the last
ingestion run. Needs no database.

One request returns every player, and the response is cached for a few minutes, so
even heavy use costs FPL only a handful of calls per hour. If FPL is unreachable the
last cached copy is served (and labelled as such).
"""

import asyncio
import logging
import time

import requests

log = logging.getLogger(__name__)

_FPL_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
_USER_AGENT = "fpl-context-mcp/0.1"
_TIMEOUT_SECONDS = 15
_CACHE_TTL_SECONDS = 300
_MAX_ROWS = 100

_STATUS_LABELS = {
    "a": "available",
    "d": "doubtful",
    "i": "injured",
    "s": "suspended",
    "u": "unavailable",
    "n": "not in squad",
}
_POSITIONS = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# Process-wide cache of the last successful bootstrap-static response.
_cache: dict = {"fetched_at": 0.0, "data": None}


def _fetch_bootstrap() -> dict:
    """Synchronous GET of bootstrap-static (run in a worker thread)."""
    resp = requests.get(_FPL_URL, timeout=_TIMEOUT_SECONDS, headers={"User-Agent": _USER_AGENT})
    resp.raise_for_status()
    return resp.json()


async def _get_bootstrap() -> tuple[dict, bool]:
    """
    Return (bootstrap-static data, is_stale).

    Serves the cache while it is younger than the TTL. On a fetch failure, falls
    back to an older cached copy (is_stale=True) and only raises when there is none.
    """
    now = time.monotonic()
    if _cache["data"] is not None and now - _cache["fetched_at"] < _CACHE_TTL_SECONDS:
        return _cache["data"], False
    try:
        data = await asyncio.to_thread(_fetch_bootstrap)
    except Exception:
        if _cache["data"] is not None:
            log.warning("FPL fetch failed; serving cached availability", exc_info=True)
            return _cache["data"], True
        raise
    _cache["data"] = data
    _cache["fetched_at"] = time.monotonic()
    return data, False


def _format_rows(rows: list[dict]) -> str:
    columns = ["player", "team", "pos", "status", "chance_of_playing", "news", "news_added"]
    header = " | ".join(columns)
    lines = [" | ".join(str(row[col]) for col in columns) for row in rows]
    return "\n".join([header, "-" * len(header), *lines])


async def get_live_availability(
    players: list[str] | None = None,
    statuses: list[str] | None = None,
) -> str:
    """
    Current FPL injury/suspension/availability flags, fetched live from FPL.

    Args:
        players:  Optional names to look up (case-insensitive substring match on first
                  name, second name or web name). Matching players are returned even
                  when fully available.
        statuses: Optional FPL status codes to keep (a/d/i/s/u/n). Defaults to every
                  player with a flag, i.e. anyone not 'a' or with a news note.

    Returns:
        A plain-text table, newest news first, capped at 100 rows; or an error or
        empty-result message.
    """
    wanted_statuses = {s.strip().lower() for s in statuses or []}
    unknown = wanted_statuses - set(_STATUS_LABELS)
    if unknown:
        return (
            f"Error: unknown status code(s) {sorted(unknown)}. "
            f"Use any of: {', '.join(f'{k} ({v})' for k, v in _STATUS_LABELS.items())}."
        )
    needles = [p.strip().lower() for p in players or [] if p and p.strip()]

    try:
        bootstrap, stale = await _get_bootstrap()
    except Exception as exc:
        log.error("Could not fetch live availability: %s", exc, exc_info=True)
        return (
            f"Error: could not reach the FPL API ({exc}). "
            "query_historical_stats has the availability stored at the last ingestion."
        )

    teams = {t["id"]: t["short_name"] for t in bootstrap.get("teams", [])}
    rows = []
    for p in bootstrap.get("elements", []):
        status = p.get("status", "a")
        news = p.get("news") or ""
        if needles:
            haystack = f"{p.get('first_name', '')} {p.get('second_name', '')} {p['web_name']}"
            if not any(n in haystack.lower() for n in needles):
                continue
        elif not wanted_statuses and status == "a" and not news:
            continue
        if wanted_statuses and status not in wanted_statuses:
            continue
        chance = p.get("chance_of_playing_next_round")
        rows.append(
            {
                "player": p["web_name"],
                "team": teams.get(p.get("team"), "?"),
                "pos": _POSITIONS.get(p.get("element_type"), "?"),
                "status": f"{status} ({_STATUS_LABELS.get(status, status)})",
                "chance_of_playing": "-" if chance is None else f"{chance}%",
                "news": news or "-",
                "news_added": (p.get("news_added") or "-"),
            }
        )

    if not rows:
        return "No players match: nobody with those filters has an availability flag."

    # Newest news first; players with no news date sort last.
    rows.sort(key=lambda r: r["news_added"] if r["news_added"] != "-" else "", reverse=True)
    total = len(rows)
    out = _format_rows(rows[:_MAX_ROWS])
    if total > _MAX_ROWS:
        out += f"\n\n({total - _MAX_ROWS} more not shown; narrow with players or statuses.)"
    if stale:
        out += "\n\nNote: FPL was unreachable, so this is a cached copy and may be out of date."
    return out
