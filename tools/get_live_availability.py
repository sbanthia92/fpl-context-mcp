"""
MCP tool: get_live_availability

Fetches every player's current FPL availability (status, chance of playing and the
news note) straight from the FPL API's bootstrap-static endpoint, so injury and
suspension flags are as fresh as FPL's own site rather than as fresh as the last
a refresh job.

One request returns every player, and the response is cached for a few minutes, so
even heavy use costs FPL only a handful of calls per hour. If FPL is unreachable the
last cached copy is served (and labelled as such).
"""

import logging

from tools import fpl_client

log = logging.getLogger(__name__)

_MAX_ROWS = 100

_STATUS_LABELS = {
    "a": "available",
    "d": "doubtful",
    "i": "injured",
    "s": "suspended",
    "u": "unavailable",
    "n": "not in squad",
}

_COLUMNS = ["player", "team", "pos", "status", "chance_of_playing", "news", "news_added"]


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
        bootstrap, stale = await fpl_client.bootstrap()
    except Exception as exc:
        log.error("Could not fetch live availability: %s", exc, exc_info=True)
        return fpl_client.unreachable_error(exc)

    teams = {t["id"]: t["short_name"] for t in bootstrap.get("teams", [])}
    rows = []
    for p in bootstrap.get("elements", []):
        status = p.get("status", "a")
        news = p.get("news") or ""
        if needles:
            if not fpl_client.player_matches(p, needles):
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
                "pos": fpl_client.POSITIONS.get(p.get("element_type"), "?"),
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
    out = fpl_client.format_table(_COLUMNS, rows[:_MAX_ROWS])
    if total > _MAX_ROWS:
        out += f"\n\n({total - _MAX_ROWS} more not shown; narrow with players or statuses.)"
    if stale:
        out += f"\n\n{fpl_client.STALE_NOTE}"
    return out
