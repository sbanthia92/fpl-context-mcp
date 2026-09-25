"""
One-time backfill: past-season player totals → PostgreSQL.

FPL's per-player endpoint (/element-summary/{id}/) includes ``history_past`` — one
row per past season with that player's season totals (points, minutes, goals,
assists, clean sheets, cards, bonus, ...). This job reads it for every player in the
current FPL squad list and writes one ``players`` row per player per past season, so
questions like "goals across the last three seasons" work.

What this does NOT give you: FPL doesn't serve past-season fixtures, teams or
per-match stats, so those tables only ever hold the current season. Past-season
``players`` rows therefore have ``team_fpl_id`` NULL, and their ``fpl_id`` is the
player's *current* FPL id. Players who have left the league aren't in the current
list, so they aren't backfilled.

Safe to re-run (upserts). Run it once after seeding, then again only if you want to
refresh past seasons (they don't change).

Run:
    fpl-context-backfill-history
    python -m jobs.backfill_history
"""

import logging
import sys
from concurrent.futures import ThreadPoolExecutor

import psycopg2.extras

from config import cfg
from jobs.ingest_match_data import _POSITION_MAP, _fpl_get, _get_db_conn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger(__name__)

_WORKERS = 8  # concurrent element-summary requests

_COLUMNS = [
    "season_id", "fpl_id", "team_fpl_id", "first_name", "second_name", "web_name",
    "position", "now_cost", "total_points", "minutes", "goals_scored", "assists",
    "clean_sheets", "goals_conceded", "yellow_cards", "red_cards", "bonus",
    "ict_index", "expected_goals", "expected_assists",
]  # fmt: skip


def _num(value) -> float | None:
    """Parse a numeric-or-string FPL field to float, None if missing/empty."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fetch_past_seasons(player: dict) -> tuple[dict, list[dict]] | None:
    """Fetch one player's history_past; None (logged) if the request fails."""
    try:
        data = _fpl_get(f"/element-summary/{player['id']}/")
    except Exception as exc:
        log.warning("history_past fetch failed for player %d: %s", player["id"], exc)
        return None
    return player, data.get("history_past", [])


def fetch_all() -> list[tuple[dict, list[dict]]]:
    """Fetch history_past for every player in the current FPL squad list."""
    log.info("Fetching FPL bootstrap-static...")
    players = _fpl_get("/bootstrap-static/").get("elements", [])
    log.info("Fetching past seasons for %d players (%d workers)...", len(players), _WORKERS)
    with ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="fpl-history") as pool:
        results = pool.map(_fetch_past_seasons, players)
    return [r for r in results if r is not None]


def _allow_null_team(cur) -> None:
    """
    Past-season rows have no team. Make players.team_fpl_id nullable if it isn't yet.

    Best-effort: databases created from an older db/schema.sql have NOT NULL here.
    Needs table ownership; if the role lacks it we log the exact statement to run.
    """
    cur.execute("SAVEPOINT allow_null_team")
    try:
        cur.execute("ALTER TABLE players ALTER COLUMN team_fpl_id DROP NOT NULL")
        cur.execute("RELEASE SAVEPOINT allow_null_team")
    except Exception as exc:
        cur.execute("ROLLBACK TO SAVEPOINT allow_null_team")
        log.warning(
            "Could not relax players.team_fpl_id (%s). If the write below fails, run "
            "this as the table owner: ALTER TABLE players ALTER COLUMN team_fpl_id "
            "DROP NOT NULL;",
            exc,
        )


def _season_id(cur, label: str, cache: dict[str, int]) -> int:
    """Return the seasons.id for a past season label, creating the row if needed."""
    if label not in cache:
        try:
            start_year = int(label.split("/")[0])
        except (ValueError, IndexError):
            start_year = 0
        cur.execute(
            """
            INSERT INTO seasons (label, start_year, is_current)
            VALUES (%s, %s, FALSE)
            ON CONFLICT (label) DO UPDATE SET start_year = EXCLUDED.start_year
            RETURNING id
            """,
            (label, start_year),
        )
        cache[label] = cur.fetchone()[0]
    return cache[label]


def write_history(conn, results: list[tuple[dict, list[dict]]]) -> int:
    """
    Upsert past-season player rows in a single transaction.

    Args:
        conn:    Open psycopg2 connection (autocommit off).
        results: Output of fetch_all().

    Returns:
        Number of player-season rows written.
    """
    with conn.cursor() as cur:
        _allow_null_team(cur)

        cur.execute("SELECT label FROM seasons WHERE is_current")
        row = cur.fetchone()
        current_label = row[0] if row else None

        season_ids: dict[str, int] = {}
        rows: dict[tuple[int, int], tuple] = {}
        for player, past_seasons in results:
            position = _POSITION_MAP.get(player.get("element_type"), "MID")
            for past in past_seasons:
                label = past.get("season_name")
                if not label or label == current_label:
                    continue  # the current season is written by ingest_match_data
                season_id = _season_id(cur, label, season_ids)
                rows[(season_id, player["id"])] = (
                    season_id,
                    player["id"],
                    None,  # team unknown for past seasons
                    player.get("first_name"),
                    player.get("second_name"),
                    player.get("web_name"),
                    position,
                    past.get("end_cost"),
                    past.get("total_points") or 0,
                    past.get("minutes") or 0,
                    past.get("goals_scored") or 0,
                    past.get("assists") or 0,
                    past.get("clean_sheets") or 0,
                    past.get("goals_conceded") or 0,
                    past.get("yellow_cards") or 0,
                    past.get("red_cards") or 0,
                    past.get("bonus") or 0,
                    _num(past.get("ict_index")),
                    _num(past.get("expected_goals")),
                    _num(past.get("expected_assists")),
                )

        if rows:
            updates = ", ".join(
                f"{c} = EXCLUDED.{c}" for c in _COLUMNS if c not in ("season_id", "fpl_id")
            )
            psycopg2.extras.execute_values(
                cur,
                f"INSERT INTO players ({', '.join(_COLUMNS)}) VALUES %s "
                f"ON CONFLICT (season_id, fpl_id) DO UPDATE SET {updates}",
                list(rows.values()),
                page_size=500,
            )

    log.info("Backfilled %d player-season rows across %d past seasons.", len(rows), len(season_ids))
    return len(rows)


def run(dry_run: bool = False) -> bool:
    """
    Backfill past-season player totals.

    Args:
        dry_run: Fetch from FPL and report what would be written, but make no
                 database connection. Can also be set with DRY_RUN=true.

    Returns:
        True on success, False if the fetch returned nothing or the write failed.
    """
    if not dry_run and not cfg.database_etl_url:
        log.error("DATABASE_ETL_URL (or DATABASE_URL) is not set — aborting backfill.")
        return False

    results = fetch_all()
    if not results:
        log.error("No player history could be fetched from FPL — aborting backfill.")
        return False

    if dry_run:
        total = sum(len(past) for _, past in results)
        log.info("[dry run] would write up to %d player-season rows. No DB writes made.", total)
        return True

    try:
        conn = _get_db_conn()
    except Exception as exc:
        log.error("Failed to open DB connection: %s", exc, exc_info=True)
        return False
    try:
        write_history(conn, results)
        conn.commit()
        log.info("Backfill committed successfully.")
        return True
    except Exception as exc:
        conn.rollback()
        log.error("Backfill failed — rolled back: %s", exc, exc_info=True)
        return False
    finally:
        conn.close()


def main() -> None:
    """CLI entry point: exit non-zero on failure so schedulers/CI flag the run as failed."""
    if not run(dry_run=cfg.dry_run):
        sys.exit(1)


if __name__ == "__main__":
    main()
