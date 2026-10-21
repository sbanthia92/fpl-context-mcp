-- Hardened read-only role for a PUBLIC hosted server (run as the database owner/admin).
--
-- query_historical_stats lets callers run arbitrary SELECTs, so on a shared endpoint the
-- database role, not the server's keyword blocklist, is what bounds the damage. This
-- gives the role access to the six FPL tables only, caps its connections, and makes
-- runaway queries die in the database even if the server is bypassed.
--
-- Replace the password. Use this role's DSN as DATABASE_URL on the hosted server;
-- keep the read/write fpl_etl role for ingestion only.

CREATE ROLE fpl_readonly WITH
    LOGIN PASSWORD 'change-me'
    NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION
    CONNECTION LIMIT 10;

-- Start from nothing: no implicit access through PUBLIC.
REVOKE ALL ON SCHEMA public FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM fpl_readonly;
GRANT USAGE ON SCHEMA public TO fpl_readonly;

GRANT SELECT ON seasons, teams, gameweeks, players, fixtures, gw_player_stats
    TO fpl_readonly;
-- Deliberately NOT: ALTER DEFAULT PRIVILEGES ... GRANT SELECT, so new tables stay private.

-- Limits that apply to every session of this role, whatever the client sends.
ALTER ROLE fpl_readonly SET default_transaction_read_only = on;
ALTER ROLE fpl_readonly SET statement_timeout = '10s';
ALTER ROLE fpl_readonly SET idle_in_transaction_session_timeout = '15s';
ALTER ROLE fpl_readonly SET lock_timeout = '2s';
ALTER ROLE fpl_readonly SET work_mem = '16MB';

-- Re-grant the schema usage the ETL role needs if you revoked PUBLIC access above:
-- GRANT USAGE ON SCHEMA public TO fpl_etl;
