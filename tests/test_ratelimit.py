"""Tests for tools/ratelimit.py and the SQL tool's concurrency cap."""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from tools import query_historical_stats as qhs
from tools.ratelimit import RateLimiter


def test_allows_up_to_limit_then_blocks_with_retry_time():
    rl = RateLimiter(2, window=60)
    assert rl.check("a") == 0 and rl.check("a") == 0
    assert 0 < rl.check("a") <= 60
    assert rl.check("b") == 0  # independent key


def test_window_resets(monkeypatch):
    import tools.ratelimit as mod

    now = [1000.0]
    monkeypatch.setattr(mod.time, "monotonic", lambda: now[0])
    rl = RateLimiter(1, window=60)
    assert rl.check("a") == 0 and rl.check("a") > 0
    now[0] += 61
    assert rl.check("a") == 0


def test_zero_limit_disables():
    rl = RateLimiter(0)
    assert all(rl.check("a") == 0 for _ in range(1000))


def test_key_table_is_bounded():
    rl = RateLimiter(5, window=60, max_keys=3)
    for i in range(10):
        rl.check(f"k{i}")
    assert len(rl._counts) <= 3


@pytest.fixture
def one_slot(monkeypatch):
    monkeypatch.setenv("MCP_MAX_CONCURRENT_QUERIES", "1")
    monkeypatch.setattr(qhs, "_slots", None)
    monkeypatch.setattr(qhs, "_SLOT_WAIT_SECONDS", 0.05)
    yield
    monkeypatch.setattr(qhs, "_slots", None)


async def test_busy_server_rejects_instead_of_queueing_forever(one_slot):
    gate = asyncio.Event()

    async def slow_connect(*args, **kwargs):
        await gate.wait()
        conn = AsyncMock()
        conn.fetch.return_value = []
        return conn

    with patch.object(qhs.asyncpg, "connect", side_effect=slow_connect):
        first = asyncio.create_task(qhs.query_historical_stats("SELECT 1"))
        await asyncio.sleep(0.01)
        second = await qhs.query_historical_stats("SELECT 2")
        gate.set()
        await first
    assert second.startswith("Error: the server is busy")


async def test_slot_is_released_after_a_query(one_slot):
    conn = AsyncMock()
    conn.fetch.return_value = []
    with patch.object(qhs.asyncpg, "connect", AsyncMock(return_value=conn)):
        await qhs.query_historical_stats("SELECT 1")
        out = await qhs.query_historical_stats("SELECT 2")
    assert out == "Query returned no results."


async def test_connection_failure_is_reported_without_details_leak(one_slot):
    with patch.object(qhs.asyncpg, "connect", AsyncMock(side_effect=OSError("secret-host:5432"))):
        out = await qhs.query_historical_stats("SELECT 1")
    assert out == "Database error: could not connect (OSError)."
    assert "secret-host" not in out
