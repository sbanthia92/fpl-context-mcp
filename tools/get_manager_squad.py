"""
MCP tool: get_manager_squad

A manager's FPL squad for a gameweek, plus the chips they've used, looked up by their
public team ID (the number in fantasy.premierleague.com/entry/<id>/...). Uses FPL's
public entry endpoints; needs no login.
"""

import logging

from tools import fpl_client

log = logging.getLogger(__name__)

_CHIP_NAMES = {
    "wildcard": "Wildcard",
    "freehit": "Free Hit",
    "bboost": "Bench Boost",
    "3xc": "Triple Captain",
    "manager": "Assistant Manager",
}

_COLUMNS = ["slot", "player", "team", "pos", "price", "pts", "form", "role", "status"]


async def get_manager_squad(team_id: int, gameweek: int | None = None) -> str:
    """
    A manager's squad and chips.

    Args:
        team_id:  The manager's FPL team ID (a whole number).
        gameweek: Gameweek to show the picks for. Defaults to the manager's current
                  gameweek.

    Returns:
        A plain-text summary with the 15-man squad, or an error message.
    """
    team_id, err = fpl_client.to_int(team_id, "team_id")
    if err:
        return err
    if team_id < 1:
        return "Error: team_id must be a positive whole number."
    if gameweek is not None:
        gameweek, err = fpl_client.to_int(gameweek, "gameweek")
        if err:
            return err
        if not 1 <= gameweek <= 38:
            return "Error: gameweek must be between 1 and 38."

    try:
        boot, stale_b = await fpl_client.bootstrap()
        entry, stale_e = await fpl_client.get(f"entry/{team_id}/", fpl_client.TTL_ENTRY)
        gw = gameweek or entry.get("current_event")
        if not gw:
            return f"Manager {team_id} has no gameweeks played yet, so there is no squad to show."
        picks, stale_p = await fpl_client.get(
            f"entry/{team_id}/event/{gw}/picks/", fpl_client.TTL_ENTRY
        )
        history, stale_h = await fpl_client.get(f"entry/{team_id}/history/", fpl_client.TTL_ENTRY)
    except fpl_client.FPLNotFound:
        return (
            f"Error: FPL has no data for team {team_id}"
            + (f" in gameweek {gameweek}" if gameweek else "")
            + ". Check the ID (it is the number in the URL of the FPL 'Points' page)."
        )
    except Exception as exc:
        log.error("Could not fetch manager %s: %s", team_id, exc, exc_info=True)
        return fpl_client.unreachable_error(exc)

    players = {p["id"]: p for p in boot.get("elements", [])}
    teams = fpl_client.teams_by_id(boot)

    rows = []
    for pick in picks.get("picks", []):
        p = players.get(pick["element"])
        if p is None:
            continue
        role = (
            "captain" if pick.get("is_captain") else "vice" if pick.get("is_vice_captain") else ""
        )
        rows.append(
            {
                "slot": pick["position"],
                "player": p["web_name"],
                "team": teams.get(p.get("team"), {}).get("short_name", "?"),
                "pos": fpl_client.POSITIONS.get(p.get("element_type"), "?"),
                "price": f"£{p['now_cost'] / 10:.1f}m",
                "pts": p.get("total_points", 0),
                "form": p.get("form", "-"),
                "role": role or ("bench" if pick["position"] > 11 else "-"),
                "status": p.get("status", "?"),
            }
        )

    summary = picks.get("entry_history") or {}
    name = f"{entry.get('player_first_name', '')} {entry.get('player_last_name', '')}".strip()
    lines = [
        f"{entry.get('name', 'Team')} (manager: {name or 'unknown'}, id {team_id}) — "
        f"gameweek {gw} squad",
        f"Overall: {entry.get('summary_overall_points', '-')} pts, rank "
        f"{entry.get('summary_overall_rank', '-')}",
    ]
    if summary:
        lines.append(
            f"This gameweek: {summary.get('points', '-')} pts, "
            f"{summary.get('event_transfers', 0)} transfer(s) "
            f"(-{summary.get('event_transfers_cost', 0)}), "
            f"bank £{summary.get('bank', 0) / 10:.1f}m, squad value "
            f"£{summary.get('value', 0) / 10:.1f}m"
        )
    active = picks.get("active_chip")
    lines.append(
        f"Chip played this gameweek: {_CHIP_NAMES.get(active, active) if active else 'none'}"
    )
    used = history.get("chips", [])
    if used:
        played = ", ".join(
            f"{_CHIP_NAMES.get(c['name'], c['name'])} (GW{c['event']})"
            for c in sorted(used, key=lambda c: c["event"])
        )
        lines.append(f"Chips used this season: {played}")
    else:
        lines.append("Chips used this season: none")
    lines += ["", fpl_client.format_table(_COLUMNS, rows)]
    lines += ["", "Slots 1-11 are the starting XI, 12-15 the bench. pts/form/price are current."]
    if stale_b or stale_e or stale_p or stale_h:
        lines += ["", fpl_client.STALE_NOTE]
    return "\n".join(lines)
