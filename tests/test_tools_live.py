"""
Tests for the live (no-database) FPL tools and the shared client.

The FPL API is mocked at ``fpl_client._fetch`` — no network access is required.
"""

from unittest.mock import patch

import pytest

from tools import fpl_client
from tools.get_current_gameweek import get_current_gameweek
from tools.get_fixture_run import get_fixture_run
from tools.get_fixtures import get_fixtures
from tools.get_manager_squad import get_manager_squad
from tools.get_xpts import estimate_xpts, get_xpts
from tools.search_players import search_players

BOOTSTRAP = {
    "teams": [
        {"id": 1, "name": "Arsenal", "short_name": "ARS"},
        {"id": 2, "name": "Man City", "short_name": "MCI"},
        {"id": 3, "name": "Man Utd", "short_name": "MUN"},
    ],
    "events": [
        {"id": 1, "name": "Gameweek 1", "finished": True, "is_current": False, "is_next": False},
        {
            "id": 2,
            "name": "Gameweek 2",
            "finished": True,
            "is_current": True,
            "is_next": False,
            "deadline_time": "2026-08-22T10:00:00Z",
            "average_entry_score": 55,
        },
        {
            "id": 3,
            "name": "Gameweek 3",
            "finished": False,
            "is_current": False,
            "is_next": True,
            "deadline_time": "2099-01-01T10:00:00Z",
        },
        {"id": 4, "name": "Gameweek 4", "finished": False, "is_current": False, "is_next": False},
    ],
    "elements": [
        {
            "id": 10,
            "web_name": "Saka",
            "first_name": "Bukayo",
            "second_name": "Saka",
            "team": 1,
            "element_type": 3,
            "now_cost": 100,
            "total_points": 50,
            "form": "6.0",
            "selected_by_percent": "40.0",
            "ep_next": "6.5",
            "points_per_game": "5.0",
            "status": "a",
        },
        {
            "id": 11,
            "web_name": "Haaland",
            "first_name": "Erling",
            "second_name": "Haaland",
            "team": 2,
            "element_type": 4,
            "now_cost": 150,
            "total_points": 80,
            "form": "8.0",
            "selected_by_percent": "60.0",
            "ep_next": "7.0",
            "points_per_game": "8.0",
            "status": "d",
        },
        {
            "id": 12,
            "web_name": "Foden",
            "first_name": "Phil",
            "second_name": "Foden",
            "team": 2,
            "element_type": 3,
            "now_cost": 80,
            "total_points": 30,
            "form": "3.0",
            "selected_by_percent": "10.0",
            "ep_next": "4.0",
            "points_per_game": "3.0",
            "status": "a",
        },
    ],
}

FIXTURES = [
    {
        "event": 2,
        "team_h": 1,
        "team_a": 2,
        "team_h_difficulty": 4,
        "team_a_difficulty": 3,
        "kickoff_time": "2026-08-23T15:00:00Z",
        "team_h_score": 1,
        "team_a_score": 2,
    },
    {
        "event": 3,
        "team_h": 2,
        "team_a": 1,
        "team_h_difficulty": 2,
        "team_a_difficulty": 5,
        "kickoff_time": "2026-08-30T15:00:00Z",
        "team_h_score": None,
        "team_a_score": None,
    },
    {
        "event": 4,
        "team_h": 1,
        "team_a": 3,
        "team_h_difficulty": 2,
        "team_a_difficulty": 4,
        "kickoff_time": None,
        "team_h_score": None,
        "team_a_score": None,
    },
]

ENTRY = {
    "name": "Test XI",
    "player_first_name": "Pat",
    "player_last_name": "Lee",
    "current_event": 2,
    "summary_overall_points": 120,
    "summary_overall_rank": 5000,
}
PICKS = {
    "active_chip": "bboost",
    "entry_history": {
        "points": 60,
        "event_transfers": 1,
        "event_transfers_cost": 4,
        "bank": 5,
        "value": 1005,
    },
    "picks": [
        {"element": 11, "position": 1, "is_captain": True, "is_vice_captain": False},
        {"element": 10, "position": 12, "is_captain": False, "is_vice_captain": True},
    ],
}
HISTORY = {"chips": [{"name": "wildcard", "event": 1}]}


def fake_fetch(path):
    if path == "bootstrap-static/":
        return BOOTSTRAP
    if path == "fixtures/":
        return FIXTURES
    if path == "entry/7/":
        return ENTRY
    if path == "entry/7/event/2/picks/":
        return PICKS
    if path == "entry/7/history/":
        return HISTORY
    raise fpl_client.FPLNotFound(path)


@pytest.fixture(autouse=True)
def mocked_fpl():
    fpl_client.clear_cache()
    with patch.object(fpl_client, "_fetch", side_effect=fake_fetch) as fetch:
        yield fetch
    fpl_client.clear_cache()


# --- shared client -----------------------------------------------------------


async def test_cache_is_shared_across_tools(mocked_fpl):
    await get_current_gameweek()
    await search_players()
    assert [c.args[0] for c in mocked_fpl.call_args_list].count("bootstrap-static/") == 1


async def test_unreachable_without_cache_returns_error():
    fpl_client.clear_cache()
    with patch.object(fpl_client, "_fetch", side_effect=RuntimeError("boom")):
        out = await get_current_gameweek()
    assert out.startswith("Error: could not reach the FPL API")


async def test_cache_evicts_oldest_when_full(monkeypatch):
    monkeypatch.setattr(fpl_client, "_MAX_CACHE_ENTRIES", 2)
    fpl_client._cache.clear()
    with patch.object(fpl_client, "_fetch", side_effect=lambda p: p):
        for path in ("a", "b", "c"):
            await fpl_client.get(path, 60)
    assert set(fpl_client._cache) == {"b", "c"}


def test_resolve_team_variants():
    assert fpl_client.resolve_team(BOOTSTRAP, "mci")[0]["id"] == 2
    assert fpl_client.resolve_team(BOOTSTRAP, "Arsenal")[0]["id"] == 1
    team, err = fpl_client.resolve_team(BOOTSTRAP, "Man")
    assert team is None and "ambiguous" in err
    team, err = fpl_client.resolve_team(BOOTSTRAP, "Zzz")
    assert team is None and "no team matches" in err


# --- get_current_gameweek ----------------------------------------------------


async def test_current_gameweek_summary():
    out = await get_current_gameweek()
    assert "Current: gameweek 2 — finished" in out
    assert "Next:" in out and "Gameweek 3" in out
    assert "2099-01-01" in out


# --- get_fixtures ------------------------------------------------------------


async def test_fixtures_default_starts_at_next_gameweek():
    out = await get_fixtures()
    assert "ARS (FDR 5)" in out  # gameweek 3, away side
    assert "1-2" not in out  # gameweek 2 is already finished
    assert "TBC" in out  # gameweek 4 has no kickoff time


async def test_fixtures_team_view_uses_that_teams_difficulty():
    out = await get_fixtures(team="Arsenal", gameweeks_ahead=2)
    lines = out.splitlines()
    gw3 = next(line for line in lines if line.startswith("3 |"))
    assert "MCI (A)" in gw3 and gw3.split(" | ")[3] == "5"


async def test_fixtures_single_gameweek_shows_score():
    out = await get_fixtures(gameweek=2)
    assert "1-2" in out


async def test_fixtures_validates_input():
    assert (await get_fixtures(gameweek=99)).startswith("Error: gameweek")
    assert (await get_fixtures(gameweek="x")).startswith("Error: gameweek")
    assert "no team matches" in await get_fixtures(team="Nope")


# --- search_players ----------------------------------------------------------


async def test_search_defaults_to_total_points_desc():
    out = await search_players()
    names = [line.split(" | ")[0] for line in out.splitlines()[2:5]]
    assert names == ["Haaland", "Saka", "Foden"]
    assert "£15.0m" in out


async def test_search_filters_and_sorting():
    out = await search_players(position="mid", max_price=9, sort_by="form")
    assert "Foden" in out and "Saka" not in out and "Haaland" not in out
    out = await search_players(team="MCI", sort_by="price")
    assert out.splitlines()[2].startswith("Haaland")
    assert "Haaland" in await search_players(name="erling")


async def test_search_validation_and_empty():
    assert (await search_players(sort_by="vibes")).startswith("Error: unknown sort_by")
    assert (await search_players(position="XXX")).startswith("Error: position")
    assert (await search_players(max_price="cheap")).startswith("Error: max_price")
    assert (await search_players(name="nobody")).startswith("No players match")


async def test_search_limit_notice():
    out = await search_players(limit=1)
    assert "2 more not shown" in out


# --- get_manager_squad -------------------------------------------------------


async def test_manager_squad_summary():
    out = await get_manager_squad(7)
    assert "Test XI" in out and "gameweek 2 squad" in out
    assert "Chip played this gameweek: Bench Boost" in out
    assert "Chips used this season: Wildcard (GW1)" in out
    assert "squad value £100.5m" in out
    assert "captain" in out and "vice" in out
    assert "(-4)" in out


async def test_manager_unknown_id():
    out = await get_manager_squad(999)
    assert "FPL has no data for team 999" in out


async def test_manager_validation():
    assert (await get_manager_squad("abc")).startswith("Error: team_id")
    assert (await get_manager_squad(0)).startswith("Error: team_id")
    assert (await get_manager_squad(7, gameweek=50)).startswith("Error: gameweek")


async def test_manager_with_no_gameweek_played():
    def fetch(path):
        return {**ENTRY, "current_event": None} if path == "entry/7/" else fake_fetch(path)

    with patch.object(fpl_client, "_fetch", side_effect=fetch):
        out = await get_manager_squad(7)
    assert "no gameweeks played" in out


# --- server wiring -----------------------------------------------------------


async def test_server_lists_and_dispatches_live_tools():
    import server

    tools = {t.name: t for t in await server.list_tools()}
    assert {
        "get_current_gameweek",
        "get_fixtures",
        "search_players",
        "get_manager_squad",
    } <= set(tools)
    assert all(t.annotations.readOnlyHint for t in tools.values())

    result = await server.call_tool("search_players", {"name": "saka"})
    assert "Saka" in result[0].text
    with pytest.raises(ValueError):
        await server.call_tool("nope", {})


async def test_query_tool_hidden_without_database(monkeypatch):
    import server

    monkeypatch.delenv("DATABASE_URL", raising=False)
    names = {t.name for t in await server.list_tools()}
    assert "query_historical_stats" not in names
    assert "get_fixtures" in names


# --- get_fixture_run ---------------------------------------------------------


async def test_fixture_run_single_team():
    out = await get_fixture_run(team="Arsenal", gameweeks=2)
    assert "Arsenal: GW3-GW4" in out
    assert "2 fixtures, average FDR 3.50, 1 home / 1 away" in out
    assert "GW3 MCI(A) 5" in out and "GW4 MUN(H) 2" in out


async def test_fixture_run_flags_blanks():
    out = await get_fixture_run(team="MCI", gameweeks=2)
    assert "Blank gameweek(s): GW4" in out


async def test_fixture_run_ranks_all_teams_easiest_first():
    out = await get_fixture_run(gameweeks=2)
    teams = [line.split(" | ")[0] for line in out.splitlines()[4:7]]
    assert teams == ["MCI", "ARS", "MUN"]  # avg FDR 2.0, 3.5, 4.0


async def test_fixture_run_double_gameweek():
    doubled = [*FIXTURES, {**FIXTURES[1], "team_h": 1, "team_a": 3}]
    with patch.object(
        fpl_client,
        "_fetch",
        side_effect=lambda p: doubled if p == "fixtures/" else fake_fetch(p),
    ):
        out = await get_fixture_run(team="Arsenal", gameweeks=1)
    assert "Double gameweek(s): GW3" in out


async def test_fixture_run_validation():
    assert (await get_fixture_run(gameweeks="x")).startswith("Error: gameweeks")
    assert "no team matches" in await get_fixture_run(team="Nope")


# --- get_xpts ----------------------------------------------------------------

STAR = {
    "element_type": 4,
    "minutes": 180,
    "status": "a",
    "chance_of_playing_next_round": None,
    "expected_goals_per_90": "0.8",
    "expected_assists_per_90": "0.2",
    "bonus": 3,
}


def test_estimate_scales_with_fixture_difficulty():
    easy = estimate_xpts(STAR, [{"fdr": 2}], 2)["xpts"]
    hard = estimate_xpts(STAR, [{"fdr": 5}], 2)["xpts"]
    assert easy > hard > 0


def test_estimate_double_and_blank_and_availability():
    single = estimate_xpts(STAR, [{"fdr": 3}], 2)["xpts"]
    assert estimate_xpts(STAR, [{"fdr": 3}, {"fdr": 3}], 2)["xpts"] == pytest.approx(2 * single)
    assert estimate_xpts(STAR, [], 2)["xpts"] == 0
    half = {**STAR, "chance_of_playing_next_round": 50}
    assert estimate_xpts(half, [{"fdr": 3}], 2)["xpts"] == pytest.approx(single / 2)
    assert estimate_xpts({**STAR, "status": "s"}, [{"fdr": 3}], 2)["xpts"] == 0
    assert estimate_xpts({**STAR, "minutes": 0}, [{"fdr": 3}], 2)["xpts"] == 0


def test_estimate_rewards_clean_sheets_for_defenders_not_forwards():
    base = {**STAR, "expected_goals_per_90": "0", "expected_assists_per_90": "0", "bonus": 0}
    cs = {"clean_sheets_per_90": "0.5"}
    defender = estimate_xpts({**base, **cs, "element_type": 2}, [{"fdr": 3}], 2)["xpts"]
    forward = estimate_xpts({**base, **cs, "element_type": 4}, [{"fdr": 3}], 2)["xpts"]
    assert defender == pytest.approx(forward + 0.5 * 4)


async def test_xpts_ranks_and_shows_fixture_and_ep_next():
    boot = {
        **BOOTSTRAP,
        "elements": [
            {**BOOTSTRAP["elements"][0], **STAR, "element_type": 3, "minutes": 180},
            {**BOOTSTRAP["elements"][1], **STAR, "expected_goals_per_90": "1.2", "minutes": 180},
            {**BOOTSTRAP["elements"][2], "minutes": 0},
        ],
    }

    def fetch(path):
        return boot if path == "bootstrap-static/" else fake_fetch(path)

    with patch.object(fpl_client, "_fetch", side_effect=fetch):
        out = await get_xpts()
    lines = out.splitlines()
    assert lines[4].startswith("Haaland") and "ARS(H) 2" in lines[4] and "7.0" in lines[4]
    assert lines[5].startswith("Saka") and "MCI(A) 5" in lines[5]
    assert "Foden" not in out  # no minutes


async def test_xpts_validation_and_blank():
    assert (await get_xpts(position="x")).startswith("Error: position")
    assert (await get_xpts(gameweek=0)).startswith("Error: gameweek")
    assert "No players with minutes" in await get_xpts()  # fixture data players have 0 minutes


async def test_server_lists_typed_tools():
    import server

    names = {t.name for t in await server.list_tools()}
    assert {"get_fixture_run", "get_xpts"} <= names
    result = await server.call_tool("get_fixture_run", {"team": "Arsenal"})
    assert "Arsenal" in result[0].text
