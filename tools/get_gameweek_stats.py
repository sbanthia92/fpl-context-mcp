"""
MCP tool: get_gameweek_stats

Every player's stats for one gameweek (default: the latest one with results), ranked, from
FPL's ``event/{gw}/live`` endpoint. Answers "who scored the most points last week" and
"which defenders kept clean sheets in GW9".
"""

import logging

from tools import fpl_client

log = logging.getLogger(__name__)

_DEFAULT_LIMIT = 15
_MAX_LIMIT = 50

SORT_FIELDS = {
    "total_points": "total_points",
    "goals_scored": "goals_scored",
    "assists": "assists",
    "bonus": "bonus",
    "minutes": "minutes",
    "expected_goals": "expected_goals",
    "expected_assists": "expected_assists",
}

COLUMNS = ["player", "team", "pos", "min", "G", "A", "CS", "bonus", "xG", "xA", "pts"]


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _latest_gameweek_with_results(boot: dict) -> int | None:
    started = [e["id"] for e in boot.get("events", []) if e.get("finished") or e.get("is_current")]
    return max(started) if started else None


async def get_gameweek_stats(
    gameweek: int | None = None,
    position: str | None = None,
    team: str | None = None,
    sort_by: str = "total_points",
    limit: int | None = None,
) -> str:
    """
    Rank players by their stats in one gameweek.

    Args:
        gameweek: 1-38. Default: the latest gameweek that has results.
        position: GKP, DEF, MID or FWD.
        team:     Team name or short name.
        sort_by:  One of SORT_FIELDS (default total_points), highest first.
        limit:    Rows to return (default 15, max 50).

    Returns:
        A plain-text table, or an error / empty-result message.
    """
    if sort_by not in SORT_FIELDS:
        return f"Error: unknown sort_by {sort_by!r}. Use one of: {', '.join(SORT_FIELDS)}."
    pos_id = None
    if position:
        pos_id = fpl_client.POSITION_IDS.get(position.strip().upper())
        if pos_id is None:
            return "Error: position must be one of GKP, DEF, MID, FWD."
    cap = _DEFAULT_LIMIT
    if limit is not None:
        cap, err = fpl_client.to_int(limit, "limit")
        if err:
            return err
        cap = max(1, min(cap, _MAX_LIMIT))
    if gameweek is not None:
        gameweek, err = fpl_client.to_int(gameweek, "gameweek")
        if err:
            return err
        if not 1 <= gameweek <= 38:
            return "Error: gameweek must be between 1 and 38."

    try:
        boot, stale_b = await fpl_client.bootstrap()
        team_id = None
        if team:
            chosen, err = fpl_client.resolve_team(boot, team)
            if err:
                return err
            team_id = chosen["id"]
        if gameweek is None:
            gameweek = _latest_gameweek_with_results(boot)
            if gameweek is None:
                return "No gameweek has results yet."
        event = next((e for e in boot.get("events", []) if e["id"] == gameweek), {})
        ttl = fpl_client.TTL_LIVE_FINISHED if event.get("finished") else fpl_client.TTL_LIVE_ACTIVE
        live, stale_l = await fpl_client.get(f"event/{gameweek}/live/", ttl)
    except Exception as exc:
        log.error("Could not fetch gameweek stats: %s", exc, exc_info=True)
        return fpl_client.unreachable_error(exc)

    players = {p["id"]: p for p in boot.get("elements", [])}
    teams = fpl_client.teams_by_id(boot)
    field = SORT_FIELDS[sort_by]
    rows = []
    for el in live.get("elements", []):
        p = players.get(el["id"])
        stats = el.get("stats", {})
        if p is None or not stats.get("minutes"):
            continue
        if team_id is not None and p.get("team") != team_id:
            continue
        if pos_id is not None and p.get("element_type") != pos_id:
            continue
        rows.append((_num(stats.get(field)), p, stats))
    if not rows:
        return f"No players with minutes match those filters in gameweek {gameweek}."
    rows.sort(key=lambda r: r[0], reverse=True)

    table = [
        {
            "player": p["web_name"],
            "team": teams.get(p.get("team"), {}).get("short_name", "?"),
            "pos": fpl_client.POSITIONS.get(p.get("element_type"), "?"),
            "min": s.get("minutes", 0),
            "G": s.get("goals_scored", 0),
            "A": s.get("assists", 0),
            "CS": s.get("clean_sheets", 0),
            "bonus": s.get("bonus", 0),
            "xG": s.get("expected_goals", "-"),
            "xA": s.get("expected_assists", "-"),
            "pts": s.get("total_points", 0),
        }
        for _, p, s in rows[:cap]
    ]
    state = "" if event.get("finished") else " (in progress: totals and bonus may still change)"
    out = f"Gameweek {gameweek} stats{state}, sorted by {sort_by}\n\n"
    out += fpl_client.format_table(COLUMNS, table)
    if len(rows) > cap:
        out += f"\n\n({len(rows) - cap} more not shown; raise limit or narrow the filters.)"
    if stale_b or stale_l:
        out += f"\n\n{fpl_client.STALE_NOTE}"
    return out
