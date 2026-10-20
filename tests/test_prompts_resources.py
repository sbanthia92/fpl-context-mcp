"""Tests for MCP prompts and resources, and the schema-drift hint in the SQL tool."""

from unittest.mock import AsyncMock, patch

import asyncpg
import pytest

import server
from tools import query_historical_stats as qhs
from tools.prompts import get_prompt, list_prompts
from tools.resources import GLOSSARY_URI, SCHEMA_URI, read_resource


def _text(result) -> str:
    return result.messages[0].content.text


def test_prompts_are_listed_with_optional_arguments():
    prompts = {p.name: p for p in list_prompts()}
    assert set(prompts) == {"captain_pick", "transfer_plan", "chip_timing"}
    for p in prompts.values():
        assert p.description and p.arguments
        assert not any(a.required for a in p.arguments)


def test_captain_prompt_names_the_tools_and_uses_team_id():
    text = _text(get_prompt("captain_pick", {"team_id": "123", "gameweek": "9"}))
    assert "team ID is 123" in text and "gameweek 9" in text
    for tool in ("get_manager_squad", "get_xpts", "get_fixture_run", "get_live_availability"):
        assert tool in text


def test_prompts_work_without_arguments():
    for name in ("captain_pick", "transfer_plan", "chip_timing"):
        text = _text(get_prompt(name, None))
        assert "ask me" in text.lower()
    assert "next 5 gameweeks" in _text(get_prompt("transfer_plan", {}))


def test_transfer_and_chip_arguments_flow_into_text():
    text = _text(get_prompt("transfer_plan", {"free_transfers": "2", "gameweeks": "8"}))
    assert "2 free transfer" in text and "gameweeks=8" in text
    text = _text(get_prompt("chip_timing", {"chip": "bboost"}))
    assert "bboost chip" in text and "Bench Boost" in text


def test_prompt_rejects_bad_input():
    with pytest.raises(ValueError):
        get_prompt("nope", {})
    with pytest.raises(ValueError):
        get_prompt("captain_pick", {"team_id": "abc"})
    with pytest.raises(ValueError):
        get_prompt("chip_timing", {"chip": "superboost"})


def test_resources_return_text():
    assert "gw_player_stats" in read_resource(SCHEMA_URI)
    assert "FDR" in read_resource(GLOSSARY_URI)
    with pytest.raises(ValueError):
        read_resource("fpl://nope")


async def test_server_exposes_prompts_and_resources():
    assert {p.name for p in await server.list_prompts()} >= {"captain_pick"}
    result = await server.get_prompt("captain_pick", {"team_id": "1"})
    assert "get_manager_squad" in _text(result)
    uris = {str(r.uri) for r in await server.list_resources()}
    assert uris == {SCHEMA_URI, GLOSSARY_URI}
    contents = await server.read_resource(SCHEMA_URI)
    assert "gw_player_stats" in contents[0].content


async def test_schema_resource_hidden_without_database(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    uris = {str(r.uri) for r in await server.list_resources()}
    assert uris == {GLOSSARY_URI}


async def test_missing_column_error_points_at_schema_resource():
    conn = AsyncMock()
    conn.fetch.side_effect = asyncpg.UndefinedColumnError('column "news_added" does not exist')
    with patch.object(qhs.asyncpg, "connect", AsyncMock(return_value=conn)):
        out = await qhs.query_historical_stats("SELECT news_added FROM players")
    assert out.startswith("Database error:")
    assert "fpl://schema" in out and "fpl-context-ingest-match" in out


async def test_other_database_errors_have_no_hint():
    conn = AsyncMock()
    conn.fetch.side_effect = asyncpg.PostgresSyntaxError("syntax error")
    with patch.object(qhs.asyncpg, "connect", AsyncMock(return_value=conn)):
        out = await qhs.query_historical_stats("SELECT FROM")
    assert "fpl://schema" not in out
