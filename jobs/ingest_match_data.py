"""
Ingestion job: match and player data → PostgreSQL.

Fetches current-season Premier League data from the FPL API and writes it to
PostgreSQL. Thread coordination is enforced via concurrent.futures.wait() so
Thread 3 (the writer) never starts until the fetch thread has finished.

Threads:
  Thread 1 — FPL API: bootstrap-static (players, teams, gameweeks) + /fixtures/
  Thread 3 — Write: upsert season/teams/gameweeks/players and ALL fixtures, then
             fetch per-player match stats only for finished fixtures that don't
             have them yet (or were played in the last couple of days)

Thread 3 starts only after Thread 1 completes. If the fetch thread fails, its
result is treated as None and the delta write is skipped.

PostgreSQL schema (read from db.py docstring and etl_v2.py):
  seasons, teams, gameweeks, players, fixtures, gw_player_stats

Run from the fpl-context-mcp directory:
    python -m jobs.ingest_match_data

Cron: see .github/workflows/ingest_match_data.yml for schedule.
"""

import logging
import sys
import traceback
from concurrent.futures import Future, ThreadPoolExecutor, wait
from datetime import datetime
from typing import Any

import psycopg2
import psycopg2.extras
import requests

from config import cfg

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

_FPL_BASE = "https://fantasy.premierleague.com/api"
_POSITION_MAP = {1: "GKP", 2: "DEF", 3: "MID", 4: "FWD"}

# psycopg2 connection options
_CONNECT_TIMEOUT = 10  # seconds

# Finished fixtures kicked off within this many days get their player stats re-fetched,
# because FPL revises bonus points and stats shortly after full time.
_STATS_REFRESH_DAYS = 2
_STATS_WORKERS = 8  # concurrent element-summary requests

_STATS_COLUMNS = [
    "season_id", "player_fpl_id", "gw_number", "fixture_fpl_id",
    "opponent_team_fpl_id", "was_home", "team_h_score", "team_a_score",
    "minutes", "goals_scored", "assists", "clean_sheets",
    "goals_conceded", "own_goals", "penalties_saved", "penalties_missed",
    "yellow_cards", "red_cards", "saves", "bonus", "bps", "total_points",
    "value", "selected", "transfers_in", "transfers_out", "transfers_balance",
    "influence", "creativity", "threat", "ict_index",
    "expected_goals", "expected_assists", "expected_goal_involvements",
    "expected_goals_conceded", "starts",
]  # fmt: skip


# ---------------------------------------------------------------------------
# HTTP helpers (synchronous — no asyncio in this module)
# ---------------------------------------------------------------------------


def _fpl_get(path: str, timeout: int = 30) -> dict:
    """
    Perform a synchronous GET request to the FPL API.

    Args:
        path:    Path relative to the FPL API base URL (e.g. '/bootstrap-static/').
        timeout: Request timeout in seconds.

    Returns:
        Parsed JSON response as a dict.

    Raises:
        requests.HTTPError: On non-2xx responses.
    """
    resp = requests.get(
        f"{_FPL_BASE}{path}",
        timeout=timeout,
        headers={"User-Agent": "fpl-context-mcp/0.1"},
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Thread 1: FPL data fetch
# ---------------------------------------------------------------------------


def fetch_fpl_data() -> dict[str, Any]:
    """
    Fetch current-season data from the FPL API.

    Retrieves bootstrap-static (players, teams, gameweeks) and the full fixture
    list in two sequential HTTP requests. Both are fast (~1–2 s each) so running
    them sequentially within this thread is preferable to adding more concurrency.

    Returns:
        Dict with keys:
          'bootstrap' — the full bootstrap-static JSON response
          'fixtures'  — the full /fixtures/ JSON response (list of fixture dicts)

    Raises:
        Exception: Any network or HTTP error is propagated to the caller
                   (ThreadPoolExecutor captures it as the Future's exception).
    """
    log.info("[Thread-FPL] fetching bootstrap-static...")
    bootstrap = _fpl_get("/bootstrap-static/")

    log.info("[Thread-FPL] fetching fixtures...")
    fixtures = _fpl_get("/fixtures/")

    log.info(
        "[Thread-FPL] done — %d players, %d fixtures.",
        len(bootstrap.get("elements", [])),
        len(fixtures),
    )
    return {"bootstrap": bootstrap, "fixtures": fixtures}


# ---------------------------------------------------------------------------
# Thread 3: Delta write to PostgreSQL
# ---------------------------------------------------------------------------


def _get_db_conn() -> psycopg2.extensions.connection:
    """
    Open and return a synchronous read/write psycopg2 connection.

    Uses DATABASE_ETL_URL (falling back to DATABASE_URL).

    Returns:
        psycopg2 connection object with autocommit disabled.

    Raises:
        RuntimeError: If no database URL is configured.
    """
    url = cfg.database_etl_url
    if not url:
        raise RuntimeError(
            "DATABASE_ETL_URL (or DATABASE_URL) must be set to run ingest_match_data."
        )
    conn = psycopg2.connect(url, connect_timeout=_CONNECT_TIMEOUT)
    conn.autocommit = False
    return conn


def _parse_dt(value: str | None) -> datetime | None:
    """
    Parse an ISO 8601 datetime string into a timezone-aware datetime.

    Args:
        value: ISO 8601 string (with or without trailing Z).

    Returns:
        Timezone-aware datetime, or None if value is empty or unparseable.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _upsert_season(cur, bootstrap: dict) -> int:
    """
    Ensure the current season row exists in the seasons table and return its id.

    Clears any stale is_current flags on other rows before marking this season
    as current — mirrors the logic in pipeline/etl_v2.py:upsert_current_season.

    Args:
        cur:       psycopg2 cursor.
        bootstrap: FPL bootstrap-static JSON response.

    Returns:
        The integer primary key of the current season row.
    """
    events = bootstrap.get("events", [])
    if not events:
        raise ValueError("bootstrap-static returned no events — cannot determine current season.")

    # Derive season label and start year from the first event's deadline_time.
    year = int(events[0]["deadline_time"][:4])
    label = f"{year}/{str(year + 1)[2:]}"

    cur.execute("UPDATE seasons SET is_current = FALSE WHERE is_current = TRUE")
    cur.execute(
        """
        INSERT INTO seasons (label, start_year, is_current)
        VALUES (%s, %s, TRUE)
        ON CONFLICT (label) DO UPDATE
            SET is_current = TRUE, start_year = EXCLUDED.start_year
        RETURNING id
        """,
        (label, year),
    )
    row = cur.fetchone()
    season_id: int = row[0]
    log.info("[Thread-Write] season: %s (id=%d)", label, season_id)
    return season_id


def _upsert_teams(cur, season_id: int, bootstrap: dict) -> None:
    """
    Upsert all PL teams from the FPL bootstrap-static response.

    Args:
        cur:       psycopg2 cursor.
        season_id: Current season primary key.
        bootstrap: FPL bootstrap-static JSON response.
    """
    teams = bootstrap.get("teams", [])
    for t in teams:
        cur.execute(
            """
            INSERT INTO teams (
                season_id, fpl_id, name, short_name, strength,
                strength_attack_home, strength_attack_away,
                strength_defence_home, strength_defence_away
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (season_id, fpl_id) DO UPDATE SET
                name = EXCLUDED.name,
                short_name = EXCLUDED.short_name,
                strength = EXCLUDED.strength,
                strength_attack_home = EXCLUDED.strength_attack_home,
                strength_attack_away = EXCLUDED.strength_attack_away,
                strength_defence_home = EXCLUDED.strength_defence_home,
                strength_defence_away = EXCLUDED.strength_defence_away
            """,
            (
                season_id,
                t["id"],
                t["name"],
                t.get("short_name"),
                t.get("strength"),
                t.get("strength_attack_home"),
                t.get("strength_attack_away"),
                t.get("strength_defence_home"),
                t.get("strength_defence_away"),
            ),
        )
    log.info("[Thread-Write] upserted %d teams.", len(teams))


def _upsert_gameweeks(cur, season_id: int, bootstrap: dict) -> None:
    """
    Upsert every gameweek from the FPL bootstrap-static ``events`` list.

    Refreshed on every run so is_current / is_next / is_finished and the average
    and highest scores stay accurate as the season progresses.

    Args:
        cur:       psycopg2 cursor.
        season_id: Current season primary key.
        bootstrap: FPL bootstrap-static JSON response.
    """
    events = bootstrap.get("events", [])
    for e in events:
        cur.execute(
            """
            INSERT INTO gameweeks (
                season_id, gw_number, deadline_time, is_current, is_next,
                is_finished, average_entry_score, highest_score
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (season_id, gw_number) DO UPDATE SET
                deadline_time = EXCLUDED.deadline_time,
                is_current = EXCLUDED.is_current,
                is_next = EXCLUDED.is_next,
                is_finished = EXCLUDED.is_finished,
                average_entry_score = EXCLUDED.average_entry_score,
                highest_score = EXCLUDED.highest_score
            """,
            (
                season_id,
                e["id"],
                _parse_dt(e.get("deadline_time")),
                bool(e.get("is_current")),
                bool(e.get("is_next")),
                bool(e.get("finished")),
                e.get("average_entry_score"),
                e.get("highest_score"),
            ),
        )
    log.info("[Thread-Write] upserted %d gameweeks.", len(events))


def _upsert_players(cur, season_id: int, bootstrap: dict) -> None:
    """
    Upsert all FPL players from the bootstrap-static response.

    Args:
        cur:       psycopg2 cursor.
        season_id: Current season primary key.
        bootstrap: FPL bootstrap-static JSON response.
    """
    players = bootstrap.get("elements", [])
    for p in players:
        position = _POSITION_MAP.get(p["element_type"], "MID")

        def _f(key: str) -> float | None:
            """Parse a string field to float, returning None if empty."""
            v = p.get(key)
            return float(v) if v else None

        cur.execute(
            """
            INSERT INTO players (
                season_id, fpl_id, team_fpl_id, first_name, second_name, web_name,
                position, now_cost, total_points, minutes, goals_scored, assists,
                clean_sheets, goals_conceded, yellow_cards, red_cards, bonus,
                form, points_per_game, selected_by_percent,
                transfers_in_event, transfers_out_event, status,
                chance_of_playing_next_round, news,
                creativity, influence, threat, ict_index,
                expected_goals, expected_assists, expected_goal_involvements,
                updated_at
            ) VALUES (
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW()
            )
            ON CONFLICT (season_id, fpl_id) DO UPDATE SET
                team_fpl_id = EXCLUDED.team_fpl_id,
                now_cost = EXCLUDED.now_cost,
                total_points = EXCLUDED.total_points,
                minutes = EXCLUDED.minutes,
                goals_scored = EXCLUDED.goals_scored,
                assists = EXCLUDED.assists,
                clean_sheets = EXCLUDED.clean_sheets,
                goals_conceded = EXCLUDED.goals_conceded,
                yellow_cards = EXCLUDED.yellow_cards,
                red_cards = EXCLUDED.red_cards,
                bonus = EXCLUDED.bonus,
                form = EXCLUDED.form,
                points_per_game = EXCLUDED.points_per_game,
                selected_by_percent = EXCLUDED.selected_by_percent,
                transfers_in_event = EXCLUDED.transfers_in_event,
                transfers_out_event = EXCLUDED.transfers_out_event,
                status = EXCLUDED.status,
                chance_of_playing_next_round = EXCLUDED.chance_of_playing_next_round,
                news = EXCLUDED.news,
                creativity = EXCLUDED.creativity,
                influence = EXCLUDED.influence,
                threat = EXCLUDED.threat,
                ict_index = EXCLUDED.ict_index,
                expected_goals = EXCLUDED.expected_goals,
                expected_assists = EXCLUDED.expected_assists,
                expected_goal_involvements = EXCLUDED.expected_goal_involvements,
                updated_at = NOW()
            """,
            (
                season_id,
                p["id"],
                p["team"],
                p["first_name"],
                p["second_name"],
                p["web_name"],
                position,
                p.get("now_cost"),
                p.get("total_points"),
                p.get("minutes"),
                p.get("goals_scored"),
                p.get("assists"),
                p.get("clean_sheets"),
                p.get("goals_conceded"),
                p.get("yellow_cards"),
                p.get("red_cards"),
                p.get("bonus"),
                _f("form"),
                _f("points_per_game"),
                _f("selected_by_percent"),
                p.get("transfers_in_event"),
                p.get("transfers_out_event"),
                p.get("status"),
                p.get("chance_of_playing_next_round"),
                p.get("news"),
                _f("creativity"),
                _f("influence"),
                _f("threat"),
                _f("ict_index"),
                _f("expected_goals"),
                _f("expected_assists"),
                _f("expected_goal_involvements"),
            ),
        )
    log.info("[Thread-Write] upserted %d players.", len(players))


def _upsert_fixtures(cur, season_id: int, fixtures: list[dict]) -> int:
    """
    Upsert every fixture in the FPL /fixtures/ response.

    All ~380 fixtures are written on every run (a trivial amount of data), so
    scores, ``finished`` flags, kickoff changes and postponements are always
    current. Fixtures without a scheduled kickoff (postponed) are skipped.

    Args:
        cur:       psycopg2 cursor.
        season_id: Current season primary key.
        fixtures:  Full list of fixture dicts from the FPL /fixtures/ endpoint.

    Returns:
        Number of fixtures upserted.
    """
    count = 0
    for f in fixtures:
        kickoff_dt = _parse_dt(f.get("kickoff_time"))
        if kickoff_dt is None:
            continue  # No scheduled kickoff (e.g. postponed without a new date)

        # Note: FPL swaps team_h_difficulty and team_a_difficulty — the figure is from
        # the *opponent's* perspective, so they are swapped when storing.
        cur.execute(
            """
            INSERT INTO fixtures (
                season_id, fpl_id, gw_number, kickoff_time,
                home_team_fpl_id, away_team_fpl_id,
                home_score, away_score, finished, started,
                home_team_difficulty, away_team_difficulty
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (season_id, fpl_id) DO UPDATE SET
                gw_number = EXCLUDED.gw_number,
                kickoff_time = EXCLUDED.kickoff_time,
                home_score = EXCLUDED.home_score,
                away_score = EXCLUDED.away_score,
                finished = EXCLUDED.finished,
                started = EXCLUDED.started,
                home_team_difficulty = EXCLUDED.home_team_difficulty,
                away_team_difficulty = EXCLUDED.away_team_difficulty
            """,
            (
                season_id,
                f["id"],
                f.get("event"),
                kickoff_dt,
                f["team_h"],
                f["team_a"],
                f.get("team_h_score"),
                f.get("team_a_score"),
                f.get("finished") or False,
                f.get("started") or False,
                f.get("team_a_difficulty"),
                f.get("team_h_difficulty"),
            ),
        )
        count += 1
    log.info("[Thread-Write] upserted %d fixtures.", count)
    return count


def _fixtures_needing_stats(cur, season_id: int) -> list[tuple]:
    """
    Find finished fixtures whose per-player stats should be (re)fetched.

    A fixture qualifies if it is finished and either has no gw_player_stats rows
    yet, or kicked off within _STATS_REFRESH_DAYS (FPL revises bonus points and
    stats shortly after full time).

    Args:
        cur:       psycopg2 cursor.
        season_id: Current season primary key.

    Returns:
        List of (fixture_fpl_id, gw_number, home_team_fpl_id, away_team_fpl_id).
    """
    cur.execute(
        """
        SELECT f.fpl_id, f.gw_number, f.home_team_fpl_id, f.away_team_fpl_id
        FROM fixtures f
        WHERE f.season_id = %s
          AND f.finished
          AND f.gw_number IS NOT NULL
          AND (
                NOT EXISTS (
                    SELECT 1 FROM gw_player_stats s
                    WHERE s.season_id = f.season_id AND s.fixture_fpl_id = f.fpl_id
                )
                OR f.kickoff_time > NOW() - make_interval(days => %s)
          )
        ORDER BY f.gw_number, f.fpl_id
        """,
        (season_id, _STATS_REFRESH_DAYS),
    )
    return cur.fetchall()


def _fetch_player_summary(player_fpl_id: int) -> dict | None:
    """Fetch one player's element-summary, returning None (and logging) on failure."""
    try:
        return _fpl_get(f"/element-summary/{player_fpl_id}/")
    except Exception as exc:
        log.warning(
            "[Thread-Write] failed to fetch element-summary for player %d: %s",
            player_fpl_id,
            exc,
        )
        return None


def _fetch_and_upsert_player_stats(cur, season_id: int, pending: list[tuple]) -> int:
    """
    Fetch per-player match stats for the given fixtures and upsert them.

    Each involved player's element-summary is fetched once (concurrently — network
    only), then every history entry belonging to one of the pending fixtures is
    written in a single batched upsert. A double-gameweek player therefore costs
    one request, not one per fixture.

    Args:
        cur:       psycopg2 cursor.
        season_id: Current season primary key.
        pending:   Rows from _fixtures_needing_stats.

    Returns:
        Number of gw_player_stats rows upserted.
    """
    fixture_ids = {row[0] for row in pending}
    team_ids = {t for row in pending for t in (row[2], row[3])}
    log.info(
        "[Thread-Write] fetching player stats for %d fixtures (%d teams)...",
        len(fixture_ids),
        len(team_ids),
    )

    cur.execute(
        "SELECT fpl_id FROM players WHERE season_id = %s AND team_fpl_id = ANY(%s)",
        (season_id, list(team_ids)),
    )
    player_ids = [r[0] for r in cur.fetchall()]

    with ThreadPoolExecutor(max_workers=_STATS_WORKERS, thread_name_prefix="fpl-stats") as pool:
        summaries = list(zip(player_ids, pool.map(_fetch_player_summary, player_ids), strict=True))

    rows: dict[tuple[int, int], tuple] = {}
    for player_fpl_id, data in summaries:
        if not data:
            continue
        for g in data.get("history", []):
            fixture_id = g.get("fixture")
            if fixture_id not in fixture_ids:
                continue

            def _gf(key: str, g: dict = g) -> float | None:
                """Parse a string field to float, returning None if empty."""
                v = g.get(key)
                return float(v) if v else None

            rows[(player_fpl_id, fixture_id)] = (
                season_id,
                player_fpl_id,
                g["round"],
                fixture_id,
                g["opponent_team"],
                g["was_home"],
                g.get("team_h_score"),
                g.get("team_a_score"),
                g.get("minutes", 0),
                g.get("goals_scored", 0),
                g.get("assists", 0),
                g.get("clean_sheets", 0),
                g.get("goals_conceded", 0),
                g.get("own_goals", 0),
                g.get("penalties_saved", 0),
                g.get("penalties_missed", 0),
                g.get("yellow_cards", 0),
                g.get("red_cards", 0),
                g.get("saves", 0),
                g.get("bonus", 0),
                g.get("bps", 0),
                g.get("total_points", 0),
                g.get("value"),
                g.get("selected"),
                g.get("transfers_in"),
                g.get("transfers_out"),
                g.get("transfers_balance"),
                _gf("influence"),
                _gf("creativity"),
                _gf("threat"),
                _gf("ict_index"),
                _gf("expected_goals"),
                _gf("expected_assists"),
                _gf("expected_goal_involvements"),
                _gf("expected_goals_conceded"),
                g.get("starts"),
            )

    if rows:
        updates = ", ".join(
            f"{c} = EXCLUDED.{c}"
            for c in _STATS_COLUMNS
            if c not in ("season_id", "player_fpl_id", "fixture_fpl_id")
        )
        psycopg2.extras.execute_values(
            cur,
            f"INSERT INTO gw_player_stats ({', '.join(_STATS_COLUMNS)}) VALUES %s "
            f"ON CONFLICT (season_id, player_fpl_id, fixture_fpl_id) DO UPDATE SET {updates}",
            list(rows.values()),
            page_size=500,
        )

    log.info("[Thread-Write] upserted %d gw_player_stats rows.", len(rows))
    return len(rows)


def delta_write(fpl_result: dict | None) -> bool:
    """
    Write the fetched FPL data to PostgreSQL in a single transaction.

    Must not be called until Thread 1 has finished (enforced by the orchestrator
    using concurrent.futures.wait).

    Execution steps:
      1. Upsert season, teams, gameweeks and players from bootstrap-static.
      2. Upsert ALL fixtures, so scores and ``finished`` flags are always current.
      3. For finished fixtures with no stats yet (or played in the last
         _STATS_REFRESH_DAYS days), fetch and upsert per-player match stats.
      4. Commit everything atomically; roll back on any error.

    The "delta" is in step 3: player stats are only fetched for fixtures that need
    them, which keeps steady-state runs to a handful of HTTP requests.

    Args:
        fpl_result: Return value of fetch_fpl_data(), or None if that thread failed.

    Returns:
        True if the write committed, False if it was aborted or rolled back.
    """
    if fpl_result is None:
        log.error("[Thread-Write] FPL fetch failed — no data to write. Aborting delta write.")
        return False

    try:
        conn = _get_db_conn()
    except Exception as exc:
        log.error("[Thread-Write] failed to open DB connection: %s", exc, exc_info=True)
        return False

    try:
        with conn.cursor() as cur:
            bootstrap = fpl_result["bootstrap"]
            fixtures = fpl_result["fixtures"]

            season_id = _upsert_season(cur, bootstrap)
            _upsert_teams(cur, season_id, bootstrap)
            _upsert_gameweeks(cur, season_id, bootstrap)
            _upsert_players(cur, season_id, bootstrap)
            _upsert_fixtures(cur, season_id, fixtures)

            pending = _fixtures_needing_stats(cur, season_id)
            if pending:
                _fetch_and_upsert_player_stats(cur, season_id, pending)
            else:
                log.info("[Thread-Write] no finished fixtures need player stats.")

        conn.commit()
        log.info("[Thread-Write] delta write committed successfully.")
        return True

    except Exception as exc:
        conn.rollback()
        log.error("[Thread-Write] delta write failed — rolling back: %s", exc, exc_info=True)
        return False
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def run(dry_run: bool = False) -> bool:
    """
    Run the full match data ingestion pipeline.

    Execution order:
      1. Submit Thread 1 (FPL fetch).
      2. Block with concurrent.futures.wait() until the fetch thread completes.
      3. Collect the result, logging full traceback if it raised.
      4. Submit Thread 3 (delta write) with the result.
      5. Wait for Thread 3 to finish.

    Thread 3 is guaranteed not to start until Thread 1 is done.

    Args:
        dry_run: When True, the fetch thread runs normally but the delta write
                 (Thread 3) is skipped. Logs the fixture and player counts that
                 would have been written so you can verify API reachability and
                 data shape before committing to a live run. Can also be enabled
                 via DRY_RUN=true.

    Returns:
        True on success, False if the fetch or the write failed.
    """
    log.info("=== ingest_match_data: starting%s ===", " [DRY RUN]" if dry_run else "")

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="ingest-match") as executor:
        # Submit fetch thread.
        f1: Future = executor.submit(fetch_fpl_data)

        # Block until the fetch thread finishes (success or failure).
        log.info("Waiting for FPL fetch thread to complete...")
        wait([f1])  # concurrent.futures.wait — returns only when all are done
        log.info("FPL fetch thread finished.")

        # Collect result, logging full traceback on failure.
        fpl_result: dict | None = None
        if f1.exception():
            exc1 = f1.exception()
            tb1 = "".join(traceback.format_exception(type(exc1), exc1, exc1.__traceback__))
            log.error("Thread-FPL raised an exception:\n%s", tb1)
        else:
            fpl_result = f1.result()

        if dry_run:
            players = len((fpl_result or {}).get("bootstrap", {}).get("elements", []))
            fixtures = len((fpl_result or {}).get("fixtures", []))
            log.info(
                "[dry run] would delta-write %d players and %d fixtures. "
                "No PostgreSQL writes made.",
                players,
                fixtures,
            )
            log.info("=== ingest_match_data: complete [DRY RUN] ===")
            return fpl_result is not None

        # Submit Thread 3 now that Thread 1 is guaranteed to be done.
        f3: Future = executor.submit(delta_write, fpl_result)
        # Wait for the write thread; re-raises on unhandled exception.
        ok = f3.result() is not False

    if not ok:
        log.error("=== ingest_match_data: FAILED — see errors above ===")
        return False

    log.info("=== ingest_match_data: complete ===")
    return True


def main() -> None:
    """CLI entry point: exit non-zero on failure so schedulers/CI flag the run as failed."""
    if not run(dry_run=cfg.dry_run):
        sys.exit(1)


if __name__ == "__main__":
    main()
