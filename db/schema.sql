-- Reference schema for standalone deployments of fpl-context-mcp.
--
-- If you're running this server against The Gaffer's existing database, you
-- don't need this file — the tables already exist there. This is only for
-- people provisioning a fresh PostgreSQL database to use with
-- query_historical_stats and jobs/ingest_match_data.py.
--
-- Columns and conflict keys match the INSERT ... ON CONFLICT statements in
-- jobs/ingest_match_data.py, so the job can write to these tables as-is.
-- Adjust types/constraints to taste — this is a starting point, not a
-- migration tool.
--
-- ingest_match_data writes the CURRENT season (teams, players, gameweeks, fixtures,
-- gw_player_stats). backfill_history adds past seasons as one `players` row per
-- player per season (season totals only; FPL doesn't serve past fixtures or
-- per-match stats), which is why players.team_fpl_id is nullable.
--
-- Upgrading a database created from an earlier version of this file:
--   ALTER TABLE players ALTER COLUMN team_fpl_id DROP NOT NULL;
--   DROP MATERIALIZED VIEW IF EXISTS player_xpts;   -- no longer used

CREATE TABLE IF NOT EXISTS seasons (
    id         SERIAL PRIMARY KEY,
    label      TEXT NOT NULL UNIQUE,      -- e.g. '2025/26'
    start_year INTEGER NOT NULL,
    is_current BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS teams (
    season_id               INTEGER NOT NULL REFERENCES seasons(id),
    fpl_id                  INTEGER NOT NULL,
    name                    TEXT NOT NULL,
    short_name              TEXT NOT NULL,
    strength                INTEGER,
    strength_attack_home    INTEGER,
    strength_attack_away    INTEGER,
    strength_defence_home   INTEGER,
    strength_defence_away   INTEGER,
    PRIMARY KEY (season_id, fpl_id)
);

CREATE TABLE IF NOT EXISTS gameweeks (
    season_id            INTEGER NOT NULL REFERENCES seasons(id),
    gw_number            INTEGER NOT NULL CHECK (gw_number BETWEEN 1 AND 38),
    deadline_time        TIMESTAMPTZ,
    is_current           BOOLEAN NOT NULL DEFAULT FALSE,
    is_next               BOOLEAN NOT NULL DEFAULT FALSE,
    is_finished           BOOLEAN NOT NULL DEFAULT FALSE,
    average_entry_score   INTEGER,
    highest_score         INTEGER,
    PRIMARY KEY (season_id, gw_number)
);

CREATE TABLE IF NOT EXISTS players (
    season_id                     INTEGER NOT NULL REFERENCES seasons(id),
    fpl_id                        INTEGER NOT NULL,
    team_fpl_id                   INTEGER,  -- NULL for past seasons (team not provided by FPL)
    first_name                    TEXT,
    second_name                   TEXT,
    web_name                      TEXT NOT NULL,
    position                      TEXT NOT NULL CHECK (position IN ('GKP', 'DEF', 'MID', 'FWD')),
    now_cost                      INTEGER,
    total_points                  INTEGER DEFAULT 0,
    minutes                       INTEGER DEFAULT 0,
    goals_scored                  INTEGER DEFAULT 0,
    assists                       INTEGER DEFAULT 0,
    clean_sheets                  INTEGER DEFAULT 0,
    goals_conceded                INTEGER DEFAULT 0,
    yellow_cards                  INTEGER DEFAULT 0,
    red_cards                     INTEGER DEFAULT 0,
    bonus                         INTEGER DEFAULT 0,
    form                          NUMERIC,
    points_per_game               NUMERIC,
    selected_by_percent           NUMERIC,
    transfers_in_event            INTEGER,
    transfers_out_event           INTEGER,
    status                        TEXT,
    chance_of_playing_next_round  INTEGER,
    news                          TEXT,
    creativity                    NUMERIC,
    influence                     NUMERIC,
    threat                        NUMERIC,
    ict_index                     NUMERIC,
    expected_goals                NUMERIC,
    expected_assists              NUMERIC,
    expected_goal_involvements    NUMERIC,
    updated_at                    TIMESTAMPTZ,
    PRIMARY KEY (season_id, fpl_id),
    FOREIGN KEY (season_id, team_fpl_id) REFERENCES teams(season_id, fpl_id)
);

CREATE TABLE IF NOT EXISTS fixtures (
    season_id             INTEGER NOT NULL REFERENCES seasons(id),
    fpl_id                INTEGER NOT NULL,
    gw_number             INTEGER,
    kickoff_time          TIMESTAMPTZ,
    home_team_fpl_id      INTEGER NOT NULL,
    away_team_fpl_id      INTEGER NOT NULL,
    home_score            INTEGER,
    away_score            INTEGER,
    finished              BOOLEAN NOT NULL DEFAULT FALSE,
    started               BOOLEAN NOT NULL DEFAULT FALSE,
    home_team_difficulty  INTEGER,
    away_team_difficulty  INTEGER,
    PRIMARY KEY (season_id, fpl_id)
);

CREATE INDEX IF NOT EXISTS idx_fixtures_kickoff_time ON fixtures (kickoff_time);

CREATE TABLE IF NOT EXISTS gw_player_stats (
    season_id                   INTEGER NOT NULL REFERENCES seasons(id),
    player_fpl_id               INTEGER NOT NULL,
    gw_number                   INTEGER NOT NULL,
    fixture_fpl_id              INTEGER NOT NULL,
    opponent_team_fpl_id        INTEGER,
    was_home                    BOOLEAN,
    team_h_score                INTEGER,
    team_a_score                INTEGER,
    minutes                     INTEGER DEFAULT 0,
    goals_scored                INTEGER DEFAULT 0,
    assists                     INTEGER DEFAULT 0,
    clean_sheets                INTEGER DEFAULT 0,
    goals_conceded              INTEGER DEFAULT 0,
    own_goals                   INTEGER DEFAULT 0,
    penalties_saved             INTEGER DEFAULT 0,
    penalties_missed            INTEGER DEFAULT 0,
    yellow_cards                INTEGER DEFAULT 0,
    red_cards                   INTEGER DEFAULT 0,
    saves                       INTEGER DEFAULT 0,
    bonus                       INTEGER DEFAULT 0,
    bps                         INTEGER DEFAULT 0,
    total_points                INTEGER DEFAULT 0,
    value                       INTEGER,
    selected                    INTEGER,
    transfers_in                INTEGER,
    transfers_out               INTEGER,
    transfers_balance           INTEGER,
    influence                   NUMERIC,
    creativity                  NUMERIC,
    threat                      NUMERIC,
    ict_index                   NUMERIC,
    expected_goals              NUMERIC,
    expected_assists            NUMERIC,
    expected_goal_involvements  NUMERIC,
    expected_goals_conceded     NUMERIC,
    starts                      INTEGER,
    -- One row per player per fixture (a double gameweek has two fixtures).
    PRIMARY KEY (season_id, player_fpl_id, fixture_fpl_id),
    FOREIGN KEY (season_id, player_fpl_id) REFERENCES players(season_id, fpl_id)
);

-- Roles used by this package (see README "Prerequisites" / config.py):
--   gaffer_readonly — used by query_historical_stats (SELECT only)
--   gaffer_etl      — used by jobs/ingest_match_data.py (SELECT + INSERT/UPDATE)
--
-- Example role setup — adjust passwords/hosts for your environment:
--
-- CREATE ROLE gaffer_readonly WITH LOGIN PASSWORD 'change-me';
-- GRANT CONNECT ON DATABASE gaffer TO gaffer_readonly;
-- GRANT USAGE ON SCHEMA public TO gaffer_readonly;
-- GRANT SELECT ON ALL TABLES IN SCHEMA public TO gaffer_readonly;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO gaffer_readonly;
--
-- CREATE ROLE gaffer_etl WITH LOGIN PASSWORD 'change-me';
-- GRANT CONNECT ON DATABASE gaffer TO gaffer_etl;
-- GRANT USAGE ON SCHEMA public TO gaffer_etl;
-- GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA public TO gaffer_etl;
-- ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE ON TABLES TO gaffer_etl;
