"""
Tests for jobs/ingest_match_data.py.

All HTTP and PostgreSQL calls are mocked. Tests cover thread coordination,
delta filtering, partial failure handling, and the full run() orchestration.
"""

from unittest.mock import MagicMock, patch

import pytest

from jobs.ingest_match_data import (
    _fetch_and_upsert_player_stats,
    _fixtures_needing_stats,
    _parse_dt,
    _upsert_fixtures,
    _upsert_gameweeks,
    delta_write,
    fetch_fpl_data,
    main,
    run,
)

# ---------------------------------------------------------------------------
# Pure helper tests
# ---------------------------------------------------------------------------


def test_parse_dt_iso():
    """_parse_dt handles ISO 8601 with trailing Z."""
    dt = _parse_dt("2026-05-10T15:00:00Z")
    assert dt is not None
    assert dt.year == 2026
    assert dt.month == 5


def test_parse_dt_none():
    """_parse_dt returns None for empty string."""
    assert _parse_dt("") is None
    assert _parse_dt(None) is None


# ---------------------------------------------------------------------------
# _upsert_fixtures — every fixture is written on every run
# ---------------------------------------------------------------------------


def _make_fixture(fpl_id: int, kickoff: str | None, finished: bool = True) -> dict:
    return {
        "id": fpl_id,
        "event": 35,
        "kickoff_time": kickoff,
        "team_h": 1,
        "team_a": 2,
        "team_h_score": 2,
        "team_a_score": 1,
        "finished": finished,
        "started": finished,
        "team_h_difficulty": 3,
        "team_a_difficulty": 4,
    }


def test_upsert_fixtures_writes_every_fixture_including_future():
    """Past AND future fixtures are all upserted — no kickoff-based delta filter.

    Regression: a MAX(kickoff_time) delta froze results after the first run, because
    the first run stored future fixtures too.
    """
    cur = MagicMock()
    fixtures = [
        _make_fixture(1, "2026-08-22T15:00:00Z"),
        _make_fixture(2, "2027-05-30T15:00:00Z", finished=False),
    ]

    count = _upsert_fixtures(cur, season_id=1, fixtures=fixtures)

    assert count == 2
    assert cur.execute.call_count == 2


def test_upsert_fixtures_skips_no_kickoff():
    """Fixtures without a kickoff_time (postponed) are skipped."""
    cur = MagicMock()
    fixtures = [{"id": 99, "kickoff_time": None, "team_h": 1, "team_a": 2}]

    assert _upsert_fixtures(cur, season_id=1, fixtures=fixtures) == 0
    cur.execute.assert_not_called()


def test_upsert_fixtures_swaps_difficulty_and_stores_scores():
    """FPL's difficulty figures are from the opponent's view, so they're swapped."""
    cur = MagicMock()
    _upsert_fixtures(cur, season_id=7, fixtures=[_make_fixture(5, "2026-09-19T14:00:00Z")])

    params = cur.execute.call_args.args[1]
    assert params[0] == 7  # season_id
    assert params[6:8] == (2, 1)  # home_score, away_score
    assert params[10:12] == (4, 3)  # home/away difficulty swapped (team_a=4, team_h=3)


def test_upsert_gameweeks_writes_each_event():
    """Every bootstrap event becomes a gameweeks row with its flags and scores."""
    cur = MagicMock()
    bootstrap = {
        "events": [
            {
                "id": 1,
                "deadline_time": "2026-08-21T17:30:00Z",
                "is_current": False,
                "is_next": False,
                "finished": True,
                "average_entry_score": 55,
                "highest_score": 130,
            },
            {"id": 2, "deadline_time": "2026-08-28T17:30:00Z", "is_current": True},
        ]
    }

    _upsert_gameweeks(cur, season_id=3, bootstrap=bootstrap)

    assert cur.execute.call_count == 2
    first = cur.execute.call_args_list[0].args[1]
    assert first[0:2] == (3, 1)
    assert first[3:] == (False, False, True, 55, 130)
    second = cur.execute.call_args_list[1].args[1]
    assert second[3] is True  # is_current


# ---------------------------------------------------------------------------
# Player stats — which fixtures, and how they're fetched
# ---------------------------------------------------------------------------


def test_fixtures_needing_stats_queries_with_refresh_window():
    """Finished fixtures lacking stats (or played recently) are selected."""
    cur = MagicMock()
    cur.fetchall.return_value = [(100, 5, 1, 2)]

    result = _fixtures_needing_stats(cur, season_id=4)

    assert result == [(100, 5, 1, 2)]
    sql, params = cur.execute.call_args.args
    assert "NOT EXISTS" in sql and "f.finished" in sql
    assert params == (4, 2)


def _history_row(fixture: int, points: int = 6) -> dict:
    return {
        "fixture": fixture,
        "round": 5,
        "opponent_team": 2,
        "was_home": True,
        "total_points": points,
        "minutes": 90,
    }


def test_fetch_player_stats_fetches_each_player_once_and_filters_fixtures():
    """One request per player; only history rows for pending fixtures are written."""
    cur = MagicMock()
    cur.fetchall.return_value = [(10,), (11,)]  # player ids on the involved teams
    pending = [(100, 5, 1, 2), (101, 6, 1, 3)]  # a "double gameweek" for team 1

    summaries = {
        10: {"history": [_history_row(100), _history_row(101), _history_row(999)]},
        11: {"history": [_history_row(100)]},
    }

    with (
        patch(
            "jobs.ingest_match_data._fpl_get",
            side_effect=lambda path, **_: summaries[int(path.strip("/").split("/")[-1])],
        ) as mock_get,
        patch("jobs.ingest_match_data.psycopg2.extras.execute_values") as mock_batch,
    ):
        written = _fetch_and_upsert_player_stats(cur, season_id=1, pending=pending)

    assert mock_get.call_count == 2  # once per player, not per fixture
    assert written == 3  # p10: fixtures 100+101 (999 ignored), p11: fixture 100
    rows = mock_batch.call_args.args[2]
    assert {(r[1], r[3]) for r in rows} == {(10, 100), (10, 101), (11, 100)}


def test_fetch_player_stats_tolerates_failed_player_requests():
    """A failing element-summary request skips that player but writes the rest."""
    cur = MagicMock()
    cur.fetchall.return_value = [(10,), (11,)]

    def fake_get(path, **_):
        if "/10/" in path:
            raise RuntimeError("timeout")
        return {"history": [_history_row(100)]}

    with (
        patch("jobs.ingest_match_data._fpl_get", side_effect=fake_get),
        patch("jobs.ingest_match_data.psycopg2.extras.execute_values") as mock_batch,
    ):
        written = _fetch_and_upsert_player_stats(cur, 1, [(100, 5, 1, 2)])

    assert written == 1
    assert mock_batch.call_args.args[2][0][1] == 11


# ---------------------------------------------------------------------------
# fetch_fpl_data
# ---------------------------------------------------------------------------


def test_fetch_fpl_data_returns_bootstrap_and_fixtures():
    """fetch_fpl_data returns both bootstrap and fixtures dicts."""
    bootstrap = {"elements": [{"id": 1}], "teams": [], "events": []}
    fixtures = [{"id": 1, "kickoff_time": "2026-05-10T15:00:00Z"}]

    with patch("jobs.ingest_match_data.requests.get") as mock_get:
        resp1 = MagicMock()
        resp1.json.return_value = bootstrap
        resp1.raise_for_status = MagicMock()
        resp2 = MagicMock()
        resp2.json.return_value = fixtures
        resp2.raise_for_status = MagicMock()
        mock_get.side_effect = [resp1, resp2]

        result = fetch_fpl_data()

    assert result["bootstrap"] == bootstrap
    assert result["fixtures"] == fixtures


# ---------------------------------------------------------------------------
# delta_write — thread coordination and partial failure
# ---------------------------------------------------------------------------


def _make_fpl_result() -> dict:
    return {
        "bootstrap": {
            "elements": [],
            "teams": [],
            "events": [{"id": 1, "deadline_time": "2026-08-01T17:30:00Z"}],
        },
        "fixtures": [],
    }


def test_delta_write_aborts_if_fpl_result_is_none():
    """delta_write logs and returns without touching the DB when FPL fetch failed."""
    with patch("jobs.ingest_match_data._get_db_conn") as mock_conn:
        delta_write(fpl_result=None)

    mock_conn.assert_not_called()


def _etl_conn() -> tuple[MagicMock, MagicMock]:
    conn = MagicMock()
    cur = MagicMock()
    cur.fetchone.return_value = (1,)  # season_id
    cur.fetchall.return_value = []  # nothing pending
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return conn, cur


def test_delta_write_commits_on_success():
    """delta_write commits when the FPL fetch succeeded."""
    conn, _ = _etl_conn()

    with patch("jobs.ingest_match_data._get_db_conn", return_value=conn):
        assert delta_write(fpl_result=_make_fpl_result()) is True

    conn.commit.assert_called_once()
    conn.rollback.assert_not_called()


def test_delta_write_rolls_back_on_error():
    """delta_write rolls back the transaction if any write step raises."""
    conn, cur = _etl_conn()
    cur.execute.side_effect = Exception("DB write failed")

    with patch("jobs.ingest_match_data._get_db_conn", return_value=conn):
        assert delta_write(fpl_result=_make_fpl_result()) is False

    conn.rollback.assert_called_once()
    conn.commit.assert_not_called()


def test_delta_write_upserts_all_fixtures_then_fetches_pending_stats():
    """All fixtures are written each run; stats are fetched only for pending fixtures."""
    conn, _ = _etl_conn()
    result = _make_fpl_result()
    result["fixtures"] = [
        _make_fixture(1, "2026-08-22T15:00:00Z"),
        _make_fixture(2, "2027-05-30T15:00:00Z", False),
    ]
    pending = [(1, 1, 1, 2)]

    with (
        patch("jobs.ingest_match_data._get_db_conn", return_value=conn),
        patch("jobs.ingest_match_data._upsert_fixtures") as mock_fx,
        patch("jobs.ingest_match_data._fixtures_needing_stats", return_value=pending),
        patch("jobs.ingest_match_data._fetch_and_upsert_player_stats") as mock_stats,
    ):
        assert delta_write(result) is True

    assert mock_fx.call_args.args[2] == result["fixtures"]  # the full list, no filtering
    assert mock_stats.call_args.args[2] == pending


def test_delta_write_skips_stats_fetch_when_nothing_pending():
    """Steady state: no finished fixture needs stats, so no player requests are made."""
    conn, _ = _etl_conn()

    with (
        patch("jobs.ingest_match_data._get_db_conn", return_value=conn),
        patch("jobs.ingest_match_data._fixtures_needing_stats", return_value=[]),
        patch("jobs.ingest_match_data._fetch_and_upsert_player_stats") as mock_stats,
    ):
        assert delta_write(_make_fpl_result()) is True

    mock_stats.assert_not_called()


def test_delta_write_returns_false_when_db_unreachable():
    """A connection failure is reported as a failed run."""
    with patch("jobs.ingest_match_data._get_db_conn", side_effect=RuntimeError("no db")):
        assert delta_write(_make_fpl_result()) is False


# ---------------------------------------------------------------------------
# run() — thread coordination
# ---------------------------------------------------------------------------


def test_run_waits_for_fetch_before_write():
    """Thread 3 (delta_write) is submitted only after wait([f1]) returns."""
    call_order = []

    def fake_fetch_fpl():
        call_order.append("fpl")
        return _make_fpl_result()

    def fake_delta_write(fpl_result):
        call_order.append("write")

    with (
        patch("jobs.ingest_match_data.fetch_fpl_data", side_effect=fake_fetch_fpl),
        patch("jobs.ingest_match_data.delta_write", side_effect=fake_delta_write),
    ):
        run()

    # Fetch thread must complete before write is called
    assert "write" in call_order
    assert call_order.index("write") > call_order.index("fpl")


def test_run_passes_none_to_write_on_fetch_failure():
    """If the fetch thread raises, delta_write receives None."""
    received: dict = {}

    def fake_delta_write(fpl_result):
        received["fpl"] = fpl_result

    with (
        patch("jobs.ingest_match_data.fetch_fpl_data", side_effect=RuntimeError("FPL down")),
        patch("jobs.ingest_match_data.delta_write", side_effect=fake_delta_write),
    ):
        run()

    assert received["fpl"] is None


# ---------------------------------------------------------------------------
# Dry-run tests
# ---------------------------------------------------------------------------


def test_run_dry_run_skips_delta_write():
    """run(dry_run=True) fetches FPL data but never calls delta_write."""
    with (
        patch("jobs.ingest_match_data.fetch_fpl_data", return_value=_make_fpl_result()),
        patch("jobs.ingest_match_data.delta_write") as mock_write,
    ):
        run(dry_run=True)

    mock_write.assert_not_called()


def test_run_dry_run_still_fetches():
    """run(dry_run=True) still calls fetch_fpl_data to verify API reachability."""
    with (
        patch("jobs.ingest_match_data.fetch_fpl_data", return_value=_make_fpl_result()) as mock_fpl,
        patch("jobs.ingest_match_data.delta_write"),
    ):
        run(dry_run=True)

    mock_fpl.assert_called_once()


# ---------------------------------------------------------------------------
# Failure signalling (exit codes)
# ---------------------------------------------------------------------------


def test_run_returns_false_when_fetch_fails():
    """A failed FPL fetch makes run() report failure rather than silent success."""
    with patch("jobs.ingest_match_data.fetch_fpl_data", side_effect=RuntimeError("boom")):
        assert run() is False


def test_main_exits_nonzero_on_failure():
    """The CLI entry point exits 1 when the job fails."""
    with patch("jobs.ingest_match_data.run", return_value=False):
        with pytest.raises(SystemExit) as exc:
            main()
    assert exc.value.code == 1
