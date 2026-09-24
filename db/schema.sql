-- Reference schema for standalone deployments of sports-context-mcp.
--
-- If you're running this server against The Gaffer's existing database, you
-- don't need this file — the tables already exist there. This is only for
-- people provisioning a fresh PostgreSQL database to use with
-- query_historical_stats and jobs/ingest_match_data.py.
--
-- Column set mirrors tools/query_historical_stats.py's SCHEMA_DESCRIPTION and
-- README.md's "Database schema" section. Adjust types/constraints to taste —
-- this is a starting point, not a migration tool.

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
    season_id         INTEGER NOT NULL REFERENCES seasons(id),
    fpl_id            INTEGER NOT NULL,
    team_fpl_id       INTEGER NOT NULL,
    first_name        TEXT,
    second_name       TEXT,
    web_name          TEXT NOT NULL,
    position          TEXT NOT NULL CHECK (position IN ('GKP', 'DEF', 'MID', 'FWD')),
    now_cost          INTEGER,
    form              NUMERIC,
    total_points      INTEGER DEFAULT 0,
    minutes           INTEGER DEFAULT 0,
    goals_scored      INTEGER DEFAULT 0,
    assists           INTEGER DEFAULT 0,
    clean_sheets      INTEGER DEFAULT 0,
    expected_goals    NUMERIC,
    expected_assists  NUMERIC,
    ict_index         NUMERIC,
    status            TEXT,
    news              TEXT,
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
    home_team_difficulty  INTEGER,
    away_team_difficulty  INTEGER,
    PRIMARY KEY (season_id, fpl_id)
);

CREATE INDEX IF NOT EXISTS idx_fixtures_kickoff_time ON fixtures (kickoff_time);

CREATE TABLE IF NOT EXISTS gw_player_stats (
    season_id             INTEGER NOT NULL REFERENCES seasons(id),
    player_fpl_id         INTEGER NOT NULL,
    gw_number             INTEGER NOT NULL,
    fixture_fpl_id        INTEGER,
    opponent_team_fpl_id  INTEGER,
    was_home              BOOLEAN,
    minutes               INTEGER DEFAULT 0,
    goals_scored          INTEGER DEFAULT 0,
    assists               INTEGER DEFAULT 0,
    clean_sheets          INTEGER DEFAULT 0,
    bonus                 INTEGER DEFAULT 0,
    total_points          INTEGER DEFAULT 0,
    expected_goals        NUMERIC,
    expected_assists      NUMERIC,
    ict_index             NUMERIC,
    starts                INTEGER DEFAULT 0,
    PRIMARY KEY (season_id, player_fpl_id, gw_number),
    FOREIGN KEY (season_id, player_fpl_id) REFERENCES players(season_id, fpl_id)
);

-- Materialized view: next-gameweek expected-points projection per player.
-- Populate/refresh this however your projection model works — this is just
-- the shape query_historical_stats expects to find.
CREATE MATERIALIZED VIEW IF NOT EXISTS player_xpts AS
SELECT
    p.fpl_id AS player_fpl_id,
    p.web_name,
    t.name AS team_name,
    p.position,
    p.now_cost,
    0::numeric AS expected_points  -- replace with your projection logic
FROM players p
JOIN teams t ON t.season_id = p.season_id AND t.fpl_id = p.team_fpl_id
JOIN seasons s ON s.id = p.season_id AND s.is_current;

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
