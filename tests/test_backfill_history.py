"""
Tests for jobs/backfill_history.py. All HTTP and PostgreSQL calls are mocked.
"""

from unittest.mock import MagicMock, patch

import pytest

from jobs.backfill_history import _allow_null_team, fetch_all, main, run, write_history


def _player(pid: int, element_type: int = 3) -> dict:
    return {
        "id": pid,
        "first_name": "First",
        "second_name": f"P{pid}",
        "web_name": f"P{pid}",
        "element_type": element_type,
    }


def _past(season: str, points: int = 100) -> dict:
    return {
        "season_name": season,
        "total_points": points,
        "minutes": 2000,
        "goals_scored": 10,
        "assists": 5,
        "end_cost": 75,
        "ict_index": "150.2",
        "expected_goals": "",
    }


def _conn(current_label: str | None, season_ids: list[int]) -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.side_effect = [(current_label,) if current_label else None] + [
        (i,) for i in season_ids
    ]
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cur


def test_write_history_skips_current_season_and_leaves_team_null():
    """Past seasons become players rows with NULL team; the current season is skipped."""
    conn, _ = _conn("2026/27", [11, 12])
    results = [(_player(7), [_past("2024/25"), _past("2023/24"), _past("2026/27")])]

    with patch("jobs.backfill_history.psycopg2.extras.execute_values") as mock_batch:
        written = write_history(conn, results)

    assert written == 2
    rows = mock_batch.call_args.args[2]
    assert {r[0] for r in rows} == {11, 12}  # two past seasons, not the current one
    assert all(r[2] is None for r in rows)  # team_fpl_id unknown
    assert rows[0][1] == 7  # fpl_id
    assert rows[0][6] == "MID"  # position mapped from element_type
    assert rows[0][7] == 75  # now_cost from end_cost
    assert rows[0][-1] is None  # empty expected_assists/goals parsed to None


def test_write_history_dedupes_per_player_season():
    """Two players in the same past season share one seasons row."""
    conn, cur = _conn(None, [11])
    results = [(_player(1), [_past("2024/25")]), (_player(2), [_past("2024/25")])]

    with patch("jobs.backfill_history.psycopg2.extras.execute_values") as mock_batch:
        assert write_history(conn, results) == 2

    season_inserts = [c for c in cur.execute.call_args_list if "INSERT INTO seasons" in c.args[0]]
    assert len(season_inserts) == 1
    assert len(mock_batch.call_args.args[2]) == 2


def test_allow_null_team_is_best_effort():
    """A failing ALTER (no ownership) is logged and rolled back to the savepoint."""
    cur = MagicMock()

    def fake_execute(sql, *_):
        if sql.startswith("ALTER TABLE"):
            raise RuntimeError("must be owner of table players")

    cur.execute.side_effect = fake_execute

    _allow_null_team(cur)  # must not raise

    statements = [c.args[0] for c in cur.execute.call_args_list]
    assert "ROLLBACK TO SAVEPOINT allow_null_team" in statements


def test_fetch_all_skips_failed_players():
    """A failing element-summary request drops that player but keeps the others."""

    def fake_get(path, **_):
        if path == "/bootstrap-static/":
            return {"elements": [_player(1), _player(2)]}
        if "/1/" in path:
            raise RuntimeError("timeout")
        return {"history_past": [_past("2024/25")]}

    with patch("jobs.backfill_history._fpl_get", side_effect=fake_get):
        results = fetch_all()

    assert [p["id"] for p, _ in results] == [2]


def test_run_dry_run_makes_no_db_connection():
    """dry_run fetches and reports but never opens a connection."""
    with (
        patch("jobs.backfill_history.fetch_all", return_value=[(_player(1), [_past("2024/25")])]),
        patch("jobs.backfill_history._get_db_conn") as mock_conn,
    ):
        assert run(dry_run=True) is True

    mock_conn.assert_not_called()


def test_run_fails_without_database_url(monkeypatch):
    """No DB URL -> failure, before any FPL request is made."""
    monkeypatch.setenv("DATABASE_ETL_URL", "")
    monkeypatch.setenv("DATABASE_URL", "")
    with patch("jobs.backfill_history.fetch_all") as mock_fetch:
        assert run() is False
    mock_fetch.assert_not_called()


def test_run_fails_when_nothing_fetched():
    """An FPL outage (no players fetched) is a failed run, not a silent success."""
    with patch("jobs.backfill_history.fetch_all", return_value=[]):
        assert run() is False


def test_run_commits_and_rolls_back():
    """A successful write commits; a failing write rolls back and reports failure."""
    conn = MagicMock()
    fetched = [(_player(1), [_past("2024/25")])]

    with (
        patch("jobs.backfill_history.fetch_all", return_value=fetched),
        patch("jobs.backfill_history._get_db_conn", return_value=conn),
        patch("jobs.backfill_history.write_history", return_value=1),
    ):
        assert run() is True
    conn.commit.assert_called_once()

    conn2 = MagicMock()
    with (
        patch("jobs.backfill_history.fetch_all", return_value=fetched),
        patch("jobs.backfill_history._get_db_conn", return_value=conn2),
        patch("jobs.backfill_history.write_history", side_effect=RuntimeError("boom")),
    ):
        assert run() is False
    conn2.rollback.assert_called_once()
    conn2.commit.assert_not_called()


def test_main_exits_nonzero_on_failure():
    """The CLI entry point exits 1 when the backfill fails."""
    with patch("jobs.backfill_history.run", return_value=False):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1
