"""
Tests for tools/get_live_availability.py.

The FPL API is mocked — no network access is required.
"""

from unittest.mock import patch

import pytest

from tools import fpl_client
from tools.get_live_availability import get_live_availability

BOOTSTRAP = {
    "teams": [{"id": 1, "short_name": "ARS"}, {"id": 2, "short_name": "MCI"}],
    "elements": [
        {
            "web_name": "Saka",
            "first_name": "Bukayo",
            "second_name": "Saka",
            "team": 1,
            "element_type": 3,
            "status": "d",
            "chance_of_playing_next_round": 75,
            "news": "Knock - 75% chance of playing",
            "news_added": "2026-10-05T10:00:00Z",
        },
        {
            "web_name": "Haaland",
            "first_name": "Erling",
            "second_name": "Haaland",
            "team": 2,
            "element_type": 4,
            "status": "a",
            "chance_of_playing_next_round": None,
            "news": "",
            "news_added": None,
        },
        {
            "web_name": "Rodri",
            "first_name": "Rodrigo",
            "second_name": "Hernandez",
            "team": 2,
            "element_type": 3,
            "status": "s",
            "chance_of_playing_next_round": 0,
            "news": "Suspended for 3 matches",
            "news_added": "2026-10-07T09:00:00Z",
        },
        {
            "web_name": "Raya",
            "first_name": "David",
            "second_name": "Raya",
            "team": 1,
            "element_type": 1,
            "status": "a",
            "chance_of_playing_next_round": 100,
            "news": "Back in training",
            "news_added": "2026-10-01T09:00:00Z",
        },
    ],
}


def _expire_cache():
    for path, (fetched_at, data) in list(fpl_client._cache.items()):
        fpl_client._cache[path] = (fetched_at - fpl_client.TTL_BOOTSTRAP - 1, data)


@pytest.fixture(autouse=True)
def reset_cache():
    fpl_client.clear_cache()
    yield
    fpl_client.clear_cache()


async def test_default_lists_flagged_players_newest_first():
    with patch.object(fpl_client, "_fetch", return_value=BOOTSTRAP):
        out = await get_live_availability()
    assert "Haaland" not in out
    names = [line.split(" | ")[0] for line in out.splitlines()[2:]]
    assert names == ["Rodri", "Saka", "Raya"]
    assert "s (suspended)" in out
    assert "75%" in out
    assert "ARS" in out


async def test_named_lookup_includes_available_players():
    with patch.object(fpl_client, "_fetch", return_value=BOOTSTRAP):
        out = await get_live_availability(players=["haaland"])
    assert "Haaland | MCI | FWD | a (available) | - | - | -" in out
    assert "Saka" not in out


async def test_name_matches_first_or_second_name():
    with patch.object(fpl_client, "_fetch", return_value=BOOTSTRAP):
        out = await get_live_availability(players=["Hernandez"])
    assert "Rodri" in out


async def test_status_filter():
    with patch.object(fpl_client, "_fetch", return_value=BOOTSTRAP):
        out = await get_live_availability(statuses=["s"])
    assert "Rodri" in out
    assert "Saka" not in out


async def test_unknown_status_is_rejected_without_fetching():
    with patch.object(fpl_client, "_fetch") as fetch:
        out = await get_live_availability(statuses=["x"])
    assert out.startswith("Error: unknown status")
    fetch.assert_not_called()


async def test_no_matches_message():
    with patch.object(fpl_client, "_fetch", return_value=BOOTSTRAP):
        out = await get_live_availability(players=["nobody"])
    assert out.startswith("No players match")


async def test_response_is_cached_between_calls():
    with patch.object(fpl_client, "_fetch", return_value=BOOTSTRAP) as fetch:
        await get_live_availability()
        await get_live_availability(players=["Saka"])
    assert fetch.call_count == 1


async def test_expired_cache_refetches():
    with patch.object(fpl_client, "_fetch", return_value=BOOTSTRAP) as fetch:
        await get_live_availability()
        _expire_cache()
        await get_live_availability()
    assert fetch.call_count == 2


async def test_failure_serves_stale_cache_with_note():
    with patch.object(fpl_client, "_fetch", return_value=BOOTSTRAP):
        await get_live_availability()
    _expire_cache()
    with patch.object(fpl_client, "_fetch", side_effect=RuntimeError("boom")):
        out = await get_live_availability()
    assert "Saka" in out
    assert "cached copy" in out


async def test_failure_without_cache_returns_error():
    with patch.object(fpl_client, "_fetch", side_effect=RuntimeError("boom")):
        out = await get_live_availability()
    assert out.startswith("Error: could not reach the FPL API")


async def test_row_cap_notice():
    many = {
        "teams": BOOTSTRAP["teams"],
        "elements": [
            {**BOOTSTRAP["elements"][0], "web_name": f"P{i}", "news_added": f"2026-10-{i % 28}"}
            for i in range(130)
        ],
    }
    with patch.object(fpl_client, "_fetch", return_value=many):
        out = await get_live_availability()
    assert "30 more not shown" in out
    assert len(out.splitlines()) < 110


async def test_server_registers_and_dispatches_tool():
    import server

    tools = await server.list_tools()
    assert "get_live_availability" in {t.name for t in tools}

    with patch.object(fpl_client, "_fetch", return_value=BOOTSTRAP):
        result = await server.call_tool("get_live_availability", {"players": ["Saka"]})
    assert "Saka" in result[0].text
