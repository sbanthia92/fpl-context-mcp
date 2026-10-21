"""
MCP tool: get_xpts

A transparent expected-points estimate for the next gameweek, built from each player's
season-to-date underlying stats (xG, xA, clean sheets, saves, bonus per 90), expected
minutes, availability and the fixture's difficulty. Shown next to FPL's own ``ep_next``
so the two can be compared. Live FPL data.

This is a deliberately simple baseline, not a forecast model: it extrapolates the season
so far, so it is noisy in the first few gameweeks and knows nothing about rotation,
tactics or set-piece changes.
"""

import logging

from tools import fpl_client
from tools.search_players import player_row

log = logging.getLogger(__name__)

_DEFAULT_LIMIT = 15
_MAX_LIMIT = 50

# FPL scoring: points per goal, per clean sheet, by position id (1 GKP .. 4 FWD).
_GOAL_PTS = {1: 10, 2: 6, 3: 5, 4: 4}
_CS_PTS = {1: 4, 2: 4, 3: 1, 4: 0}
_ASSIST_PTS = 3
_FDR_STEP = 0.1  # +/-10% per FDR point away from a neutral 3

COLUMNS = ["player", "team", "pos", "price", "fixture", "exp_min", "xPts", "ep_next", "status"]


def _f(p: dict, key: str) -> float:
    try:
        return float(p.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def estimate_xpts(p: dict, fixtures: list[dict], gws_played: int) -> dict:
    """
    Expected points for one player over the given fixtures (usually one gameweek).

    Args:
        p:         bootstrap-static element.
        fixtures:  [{'fdr': int}, ...] for the team's matches in the target gameweek;
                   empty means a blank gameweek.
        gws_played: finished gameweeks so far (the sample the per-90 rates come from).

    Returns:
        {'xpts', 'exp_min'} where exp_min is expected minutes per match.
    """
    pos = p.get("element_type")
    minutes = _f(p, "minutes")
    if not fixtures or minutes <= 0 or pos not in _GOAL_PTS:
        return {"xpts": 0.0, "exp_min": 0.0}

    # Share of available minutes actually played, capped at a full 90.
    exp_min = 90 * min(1.0, minutes / (90 * max(gws_played, 1)))
    chance = p.get("chance_of_playing_next_round")
    chance = 100 if chance is None else chance
    if p.get("status") in ("s", "u", "n"):
        chance = 0
    avail = chance / 100

    share = exp_min / 90
    per90 = minutes / 90
    appearance = 2 * min(1.0, exp_min / 60)
    goals = _f(p, "expected_goals_per_90") * _GOAL_PTS[pos] * share
    assists = _f(p, "expected_assists_per_90") * _ASSIST_PTS * share
    clean_sheet = _f(p, "clean_sheets_per_90") * _CS_PTS[pos] * min(1.0, exp_min / 60)
    conceded = 0.0
    saves = 0.0
    if pos in (1, 2):
        conceded = -0.5 * _f(p, "expected_goals_conceded_per_90") * share
    if pos == 1:
        saves = _f(p, "saves_per_90") / 3 * share
    bonus = (_f(p, "bonus") / per90) * share if per90 else 0.0

    base = appearance + goals + assists + clean_sheet + conceded + saves + bonus
    total = 0.0
    for fx in fixtures:
        total += base * (1 + (3 - fx["fdr"]) * _FDR_STEP)
    return {"xpts": total * avail, "exp_min": exp_min * avail}


async def get_xpts(
    position: str | None = None,
    team: str | None = None,
    max_price: float | None = None,
    gameweek: int | None = None,
    limit: int | None = None,
) -> str:
    """
    Rank players by estimated expected points for a gameweek.

    Args:
        position:  GKP, DEF, MID or FWD.
        team:      Team name or short name.
        max_price: Maximum price in £m.
        gameweek:  Gameweek to project (default: the next one to play).
        limit:     Rows to return (default 15, max 50).

    Returns:
        A plain-text table, or an error / empty-result message.
    """
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
        log.error("Could not fetch data for xPts: %s", exc, exc_info=True)
        return fpl_client.unreachable_error(exc)

    team_id = None
    if team:
        chosen, err = fpl_client.resolve_team(boot, team)
        if err:
            return err
        team_id = chosen["id"]
    if gameweek is None:
        nxt = fpl_client.current_gameweek(boot)
        if nxt is None:
            return "No upcoming gameweeks: the season is over."
        gameweek = nxt["id"]

    teams = fpl_client.teams_by_id(boot)
    gws_played = sum(1 for e in boot.get("events", []) if e.get("finished"))
    by_team: dict[int, list[dict]] = {}
    labels: dict[int, list[str]] = {}
    for f in fixtures:
        if f.get("event") != gameweek:
            continue
        for tid, opp, home, fdr in (
            (f["team_h"], f["team_a"], True, f["team_h_difficulty"]),
            (f["team_a"], f["team_h"], False, f["team_a_difficulty"]),
        ):
            by_team.setdefault(tid, []).append({"fdr": fdr})
            labels.setdefault(tid, []).append(
                f"{teams[opp]['short_name']}({'H' if home else 'A'}) {fdr}"
            )

    scored = []
    for p in boot.get("elements", []):
        if team_id is not None and p.get("team") != team_id:
            continue
        if pos_id is not None and p.get("element_type") != pos_id:
            continue
        if price_cap is not None and p["now_cost"] > round(price_cap * 10):
            continue
        est = estimate_xpts(p, by_team.get(p.get("team"), []), gws_played)
        if est["xpts"] > 0:
            scored.append((est, p))
    if not scored:
        return "No players with minutes match those filters (or the team has a blank gameweek)."
    scored.sort(key=lambda item: item[0]["xpts"], reverse=True)

    rows = []
    for est, p in scored[:cap]:
        row = player_row(p, teams)
        rows.append(
            {
                "player": row["player"],
                "team": row["team"],
                "pos": row["pos"],
                "price": row["price"],
                "fixture": ", ".join(labels.get(p.get("team"), [])) or "blank",
                "exp_min": f"{est['exp_min']:.0f}",
                "xPts": f"{est['xpts']:.2f}",
                "ep_next": p.get("ep_next", "-"),
                "status": row["status"],
            }
        )
    out = f"Estimated expected points, gameweek {gameweek}\n\n"
    out += fpl_client.format_table(COLUMNS, rows)
    if len(scored) > cap:
        out += f"\n\n({len(scored) - cap} more not shown; raise limit or narrow the filters.)"
    out += (
        "\n\nxPts is a simple baseline from season-to-date xG/xA/clean-sheet/save/bonus "
        "rates, expected minutes, availability and fixture difficulty (fixture shows "
        "opponent and its FDR). ep_next is FPL's own figure. Rates are noisy early in the "
        "season and ignore rotation risk, so treat both as one input among several."
    )
    if gws_played < 3:
        out += f" Only {gws_played} gameweek(s) played so far: treat the estimates as very rough."
    if stale_b or stale_f:
        out += f"\n\n{fpl_client.STALE_NOTE}"
    return out
