"""
MCP tool: get_fixture_run

How easy or hard a team's next few fixtures are, summarised: average FDR, home/away
split, blanks and doubles. With no team it ranks every team (a fixture ticker), easiest
run first. Live FPL data.
"""

import logging

from tools import fpl_client

log = logging.getLogger(__name__)

_DEFAULT_WINDOW = 5


def build_runs(
    fixtures: list[dict], teams: dict[int, dict], start: int, window: int
) -> dict[int, list[dict]]:
    """Per-team list of {gw, opp, home, fdr} for gameweeks start..start+window-1."""
    wanted = range(start, min(start + window, 39))
    runs: dict[int, list[dict]] = {tid: [] for tid in teams}
    for f in fixtures:
        gw = f.get("event")
        if gw not in wanted:
            continue
        for tid, opp, home, fdr in (
            (f["team_h"], f["team_a"], True, f["team_h_difficulty"]),
            (f["team_a"], f["team_h"], False, f["team_a_difficulty"]),
        ):
            if tid in runs:
                runs[tid].append(
                    {"gw": gw, "opp": teams[opp]["short_name"], "home": home, "fdr": fdr}
                )
    for games in runs.values():
        games.sort(key=lambda g: g["gw"])
    return runs


def summarise(games: list[dict], start: int, window: int) -> dict:
    gws = list(range(start, min(start + window, 39)))
    played = {g["gw"] for g in games}
    blanks = [gw for gw in gws if gw not in played]
    doubles = sorted({g["gw"] for g in games if sum(1 for x in games if x["gw"] == g["gw"]) > 1})
    avg = sum(g["fdr"] for g in games) / len(games) if games else None
    return {
        "avg_fdr": avg,
        "games": len(games),
        "home": sum(1 for g in games if g["home"]),
        "blanks": blanks,
        "doubles": doubles,
    }


def _sequence(games: list[dict]) -> str:
    return ", ".join(
        f"GW{g['gw']} {g['opp']}({'H' if g['home'] else 'A'}) {g['fdr']}" for g in games
    )


async def get_fixture_run(team: str | None = None, gameweeks: int | None = None) -> str:
    """
    Summarise upcoming fixture difficulty.

    Args:
        team:      Team name or short name. Omit to rank all 20 teams.
        gameweeks: Window length starting from the next gameweek to play (default 5).

    Returns:
        A plain-text summary or ranking, or an error message.
    """
    window = _DEFAULT_WINDOW
    if gameweeks is not None:
        window, err = fpl_client.to_int(gameweeks, "gameweeks")
        if err:
            return err
        window = max(1, min(window, 38))

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
    nxt = fpl_client.current_gameweek(boot)
    if nxt is None:
        return "No upcoming gameweeks: the season is over."
    start = nxt["id"]

    runs = build_runs(fixtures, teams, start, window)
    stale = f"\n\n{fpl_client.STALE_NOTE}" if stale_b or stale_f else ""
    end = min(start + window - 1, 38)

    if chosen is not None:
        s = summarise(runs[chosen["id"]], start, window)
        lines = [f"{chosen['name']}: GW{start}-GW{end}"]
        if s["avg_fdr"] is None:
            lines.append("No fixtures in this window.")
        else:
            lines.append(
                f"{s['games']} fixtures, average FDR {s['avg_fdr']:.2f}, {s['home']} home / "
                f"{s['games'] - s['home']} away"
            )
            lines.append(_sequence(runs[chosen["id"]]))
        if s["blanks"]:
            lines.append("Blank gameweek(s): " + ", ".join(f"GW{g}" for g in s["blanks"]))
        if s["doubles"]:
            lines.append("Double gameweek(s): " + ", ".join(f"GW{g}" for g in s["doubles"]))
        lines.append("FDR: 1 easiest, 5 hardest, from this team's point of view.")
        return "\n".join(lines) + stale

    rows = []
    for tid, games in runs.items():
        s = summarise(games, start, window)
        rows.append(
            {
                "team": teams[tid]["short_name"],
                "avg_fdr": "-" if s["avg_fdr"] is None else f"{s['avg_fdr']:.2f}",
                "games": s["games"],
                "home": s["home"],
                "blank_gws": ",".join(map(str, s["blanks"])) or "-",
                "double_gws": ",".join(map(str, s["doubles"])) or "-",
                "_sort": s["avg_fdr"] if s["avg_fdr"] is not None else 99,
            }
        )
    rows.sort(key=lambda r: (r["_sort"], r["team"]))
    cols = ["team", "avg_fdr", "games", "home", "blank_gws", "double_gws"]
    return (
        f"Fixture run GW{start}-GW{end}, easiest first\n\n"
        + fpl_client.format_table(cols, rows)
        + "\n\nFDR: 1 easiest, 5 hardest. Pass a team for its fixture-by-fixture run."
        + stale
    )
