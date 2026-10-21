"""
MCP tool: get_player_history

One player's match-by-match record this season (optionally against a single opponent) and
their season totals for past seasons, from FPL's per-player ``element-summary`` endpoint.


FPL serves per-match data for the CURRENT season only; past seasons are season totals
(and only for players still in the game's player list). The output says so when an
opponent filter is used, so nobody mistakes "no past rows" for "never played them".
"""

import logging

from tools import fpl_client

log = logging.getLogger(__name__)

_MAX_MATCHES = 40

_MATCH_COLUMNS = ["gw", "opponent", "score", "min", "G", "A", "CS", "bonus", "xG", "xA", "pts"]
_SEASON_COLUMNS = ["season", "pts", "min", "G", "A", "CS", "bonus", "start", "end"]


def _score(m: dict) -> str:
    if m.get("team_h_score") is None or m.get("team_a_score") is None:
        return "-"
    return f"{m['team_h_score']}-{m['team_a_score']}"


def _match_row(m: dict, teams: dict[int, dict]) -> dict:
    opp = teams.get(m["opponent_team"], {}).get("short_name", "?")
    return {
        "gw": m["round"],
        "opponent": f"{opp} ({'H' if m['was_home'] else 'A'})",
        "score": _score(m),
        "min": m["minutes"],
        "G": m["goals_scored"],
        "A": m["assists"],
        "CS": m["clean_sheets"],
        "bonus": m["bonus"],
        "xG": m.get("expected_goals", "-"),
        "xA": m.get("expected_assists", "-"),
        "pts": m["total_points"],
    }


def _season_row(s: dict) -> dict:
    return {
        "season": s["season_name"],
        "pts": s["total_points"],
        "min": s["minutes"],
        "G": s["goals_scored"],
        "A": s["assists"],
        "CS": s["clean_sheets"],
        "bonus": s["bonus"],
        "start": f"£{s['start_cost'] / 10:.1f}m",
        "end": f"£{s['end_cost'] / 10:.1f}m",
    }


async def get_player_history(
    player: str,
    opponent: str | None = None,
    last_n: int | None = None,
) -> str:
    """
    A player's recent matches and past-season totals.

    Args:
        player:   Player name (first, second or web name; 'Saka', 'Bukayo Saka').
        opponent: Optional team name or short name: only this season's matches against them.
        last_n:   Optional: only the most recent N matches (applied after the opponent filter).

    Returns:
        A plain-text report, or an error message.
    """
    count = None
    if last_n is not None:
        count, err = fpl_client.to_int(last_n, "last_n")
        if err:
            return err
        count = max(1, min(count, _MAX_MATCHES))

    try:
        boot, stale_b = await fpl_client.bootstrap()
        found, err = fpl_client.find_player(boot, player)
        if err:
            return err
        opp = None
        if opponent:
            opp, err = fpl_client.resolve_team(boot, opponent)
            if err:
                return err
        summary, stale_s = await fpl_client.get(
            f"element-summary/{found['id']}/", fpl_client.TTL_ELEMENT
        )
    except Exception as exc:
        log.error("Could not fetch player history: %s", exc, exc_info=True)
        return fpl_client.unreachable_error(exc)

    teams = fpl_client.teams_by_id(boot)
    matches = sorted(summary.get("history", []), key=lambda m: (m["round"], m.get("kickoff_time")))
    if opp is not None:
        matches = [m for m in matches if m["opponent_team"] == opp["id"]]
    if count is not None:
        matches = matches[-count:]
    past = summary.get("history_past", [])

    team = teams.get(found["team"], {})
    lines = [
        f"{found['first_name']} {found['second_name']} ({found['web_name']}) — "
        f"{team.get('name', '?')}, {fpl_client.POSITIONS.get(found['element_type'], '?')}, "
        f"£{found['now_cost'] / 10:.1f}m",
        f"This season: {found.get('total_points', 0)} pts, {found.get('minutes', 0)} min, "
        f"{found.get('goals_scored', 0)} G, {found.get('assists', 0)} A, form "
        f"{found.get('form', '-')}",
        "",
    ]

    heading = "Matches this season"
    if opp is not None:
        heading += f" against {opp['name']}"
    if count is not None:
        heading += f" (last {count})"
    if matches:
        lines.append(heading + ":")
        lines.append(
            fpl_client.format_table(_MATCH_COLUMNS, [_match_row(m, teams) for m in matches])
        )
        played = [m for m in matches if m["minutes"] > 0]
        lines.append(
            f"Total over {len(matches)} match(es): "
            f"{sum(m['total_points'] for m in matches)} pts, "
            f"{sum(m['goals_scored'] for m in matches)} G, "
            f"{sum(m['assists'] for m in matches)} A, "
            f"{sum(m['clean_sheets'] for m in matches)} CS in {len(played)} appearance(s)"
        )
    else:
        suffix = f" against {opp['name']}" if opp is not None else ""
        lines.append(f"No matches this season{suffix} yet.")

    lines.append("")
    if past:
        lines.append("Past seasons (season totals):")
        lines.append(fpl_client.format_table(_SEASON_COLUMNS, [_season_row(s) for s in past]))
    else:
        lines.append("No past seasons on record (new to the game, or first season).")
    lines.append("")
    lines.append(
        "FPL provides match-by-match data for the current season only; past seasons are totals."
    )
    if opp is not None:
        lines.append(
            f"So results against {opp['name']} in earlier seasons are not available from FPL."
        )
    if stale_b or stale_s:
        lines += ["", fpl_client.STALE_NOTE]
    return "\n".join(lines)
