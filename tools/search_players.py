"""
MCP tool: search_players

Find and rank current-season FPL players by name, team, position and price, using the
live bootstrap-static data. Needs no database.
"""

import logging

from tools import fpl_client

log = logging.getLogger(__name__)

_DEFAULT_LIMIT = 15
_MAX_LIMIT = 50

# sort key -> (bootstrap field, description). All are numeric once cast to float.
SORT_FIELDS = {
    "total_points": "total_points",
    "form": "form",
    "price": "now_cost",
    "selected_by_percent": "selected_by_percent",
    "ep_next": "ep_next",
    "points_per_game": "points_per_game",
    "goals_scored": "goals_scored",
    "assists": "assists",
    "clean_sheets": "clean_sheets",
    "minutes": "minutes",
    "transfers_in_event": "transfers_in_event",
}

COLUMNS = ["player", "team", "pos", "price", "pts", "form", "sel%", "ep_next", "status"]


def _num(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def player_row(p: dict, teams: dict[int, dict]) -> dict:
    return {
        "player": p["web_name"],
        "team": teams.get(p.get("team"), {}).get("short_name", "?"),
        "pos": fpl_client.POSITIONS.get(p.get("element_type"), "?"),
        "price": f"£{p['now_cost'] / 10:.1f}m",
        "pts": p.get("total_points", 0),
        "form": p.get("form", "-"),
        "sel%": p.get("selected_by_percent", "-"),
        "ep_next": p.get("ep_next", "-"),
        "status": p.get("status", "?"),
    }


async def search_players(
    name: str | None = None,
    team: str | None = None,
    position: str | None = None,
    max_price: float | None = None,
    sort_by: str = "total_points",
    limit: int | None = None,
) -> str:
    """
    Search and rank current-season players.

    Args:
        name:      Substring of first, second or web name (case-insensitive).
        team:      Team name or short name.
        position:  GKP, DEF, MID or FWD.
        max_price: Maximum price in £m, e.g. 7.5.
        sort_by:   One of SORT_FIELDS (default total_points), highest first.
        limit:     Rows to return (default 15, max 50).

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
    price_cap = None
    if max_price is not None:
        try:
            price_cap = float(max_price)
        except (TypeError, ValueError):
            return f"Error: max_price must be a number, got {max_price!r}."

    try:
        boot, stale = await fpl_client.bootstrap()
    except Exception as exc:
        log.error("Could not fetch players: %s", exc, exc_info=True)
        return fpl_client.unreachable_error(exc)

    team_id = None
    if team:
        chosen, err = fpl_client.resolve_team(boot, team)
        if err:
            return err
        team_id = chosen["id"]

    needles = [name.strip().lower()] if name and name.strip() else []
    field = SORT_FIELDS[sort_by]
    matches = [
        p
        for p in boot.get("elements", [])
        if (not needles or fpl_client.player_matches(p, needles))
        and (team_id is None or p.get("team") == team_id)
        and (pos_id is None or p.get("element_type") == pos_id)
        and (price_cap is None or p["now_cost"] <= round(price_cap * 10))
    ]
    if not matches:
        return "No players match those filters."
    matches.sort(key=lambda p: _num(p.get(field)), reverse=True)

    teams = fpl_client.teams_by_id(boot)
    rows = [player_row(p, teams) for p in matches[:cap]]
    out = fpl_client.format_table(COLUMNS, rows)
    if len(matches) > cap:
        out += f"\n\n({len(matches) - cap} more not shown; raise limit or narrow the filters.)"
    out += f"\n\nSorted by {sort_by}, highest first. Status: a available, d doubtful, i injured, "
    out += "s suspended, u unavailable, n not in squad."
    if stale:
        out += f"\n\n{fpl_client.STALE_NOTE}"
    return out
