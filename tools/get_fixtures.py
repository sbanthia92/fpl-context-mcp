"""
MCP tool: get_fixtures

Fixtures with FPL's fixture difficulty rating (FDR, 1 easiest to 5 hardest) from the
live FPL fixtures endpoint.
"""

import logging
from datetime import datetime

from tools import fpl_client

log = logging.getLogger(__name__)

_DEFAULT_AHEAD = 5
_MAX_ROWS = 100


def _kickoff(value: str | None) -> str:
    if not value:
        return "TBC"
    return datetime.fromisoformat(value.replace("Z", "+00:00")).strftime("%a %d %b %H:%M")


def _score(fx: dict) -> str:
    if fx.get("team_h_score") is None or fx.get("team_a_score") is None:
        return "-"
    return f"{fx['team_h_score']}-{fx['team_a_score']}"


async def get_fixtures(
    team: str | None = None,
    gameweek: int | None = None,
    gameweeks_ahead: int | None = None,
) -> str:
    """
    Fixtures with difficulty ratings.

    Args:
        team:            Optional team name or short name ('Arsenal', 'MCI'). When set,
                         rows are shown from that team's point of view.
        gameweek:        A single gameweek (1-38). Overrides gameweeks_ahead.
        gameweeks_ahead: How many gameweeks to list starting from the next one to play
                         (default 5). Ignored when gameweek is given.

    Returns:
        A plain-text table, or an error / empty-result message.
    """
    ahead = _DEFAULT_AHEAD
    if gameweeks_ahead is not None:
        ahead, err = fpl_client.to_int(gameweeks_ahead, "gameweeks_ahead")
        if err:
            return err
        ahead = max(1, min(ahead, 38))
    if gameweek is not None:
        gameweek, err = fpl_client.to_int(gameweek, "gameweek")
        if err:
            return err
        if not 1 <= gameweek <= 38:
            return "Error: gameweek must be between 1 and 38."

    try:
        boot, stale_b = await fpl_client.bootstrap()
        fixtures, stale_f = await fpl_client.fixtures()
    except Exception as exc:
        log.error("Could not fetch fixtures: %s", exc, exc_info=True)
        return fpl_client.unreachable_error(exc)

    teams = fpl_client.teams_by_id(boot)
    chosen = None
    if team:
        chosen, err = fpl_client.resolve_team(boot, team)
        if err:
            return err

    if gameweek is not None:
        wanted = {gameweek}
    else:
        nxt = fpl_client.current_gameweek(boot)
        if nxt is None:
            return "No upcoming gameweeks: the season is over."
        # If the current gameweek is still in progress, include it.
        start = nxt["id"]
        wanted = set(range(start, start + ahead))

    selected = [
        f
        for f in fixtures
        if f.get("event") in wanted
        and (chosen is None or chosen["id"] in (f["team_h"], f["team_a"]))
    ]
    selected.sort(key=lambda f: (f["event"], f.get("kickoff_time") or ""))
    if not selected:
        return "No fixtures match those filters."

    rows = []
    for f in selected:
        h, a = teams[f["team_h"]]["short_name"], teams[f["team_a"]]["short_name"]
        if chosen is not None:
            home = f["team_h"] == chosen["id"]
            rows.append(
                {
                    "gw": f["event"],
                    "kickoff": _kickoff(f.get("kickoff_time")),
                    "opponent": f"{a if home else h} ({'H' if home else 'A'})",
                    "fdr": f["team_h_difficulty"] if home else f["team_a_difficulty"],
                    "result": _score(f),
                }
            )
        else:
            rows.append(
                {
                    "gw": f["event"],
                    "kickoff": _kickoff(f.get("kickoff_time")),
                    "home": f"{h} (FDR {f['team_h_difficulty']})",
                    "away": f"{a} (FDR {f['team_a_difficulty']})",
                    "result": _score(f),
                }
            )
    columns = list(rows[0])
    out = fpl_client.format_table(columns, rows[:_MAX_ROWS])
    if len(rows) > _MAX_ROWS:
        out += f"\n\n({len(rows) - _MAX_ROWS} more not shown; narrow with team or gameweek.)"
    out += "\n\nFDR: 1 easiest, 5 hardest, from the team's own point of view."
    if stale_b or stale_f:
        out += f"\n\n{fpl_client.STALE_NOTE}"
    return out
