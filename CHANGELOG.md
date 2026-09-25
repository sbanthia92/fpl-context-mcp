# Changelog

All notable changes to sports-context-mcp will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.3.0] — 2026-09-24

### Added

- **PyPI packaging metadata** — `pyproject.toml` now declares `authors`,
  `license` (MIT), `readme`, `classifiers`, and `[project.urls]` so the
  package renders correctly on PyPI.
- **`[project.scripts]` entry points** — `sports-context-mcp` (the MCP
  server, `server:main`), `sports-context-ingest-press`
  (`jobs.ingest_press_content:run`), and `sports-context-ingest-match`
  (`jobs.ingest_match_data:run`) are now installed as real CLI commands, so
  a `pip install` alone is enough to run and cron-schedule the ingestion
  jobs without cloning the repo.
- **`LICENSE`** — MIT license.
- **`.env.example`** — template for local `.env` setup, listing all
  supported variables with placeholder values.
- **`db/schema.sql`** — reference PostgreSQL schema (tables + example
  read-only/ETL role grants) for anyone provisioning a database for this
  server outside of The Gaffer.
- **`.github/workflows/publish.yml`** — builds sdist/wheel and publishes to
  PyPI via Trusted Publishing (OIDC) on `v*.*.*` tag push.
- **README overhaul** — added a linear Quickstart, a "Provisioning your own
  database" section, a "Seeding data (required before first use)" section,
  and a "Keeping data fresh (ongoing)" section covering cron cadence and
  scheduling options, including a copy-paste GitHub Actions workflow for a
  customer's own private repo (no fork needed). Data staleness/emptiness was previously undocumented
  as an ongoing operational requirement.

### Fixed

- **`db/schema.sql` now matches `jobs/ingest_match_data.py`.** The first draft
  was missing about 40 columns the job writes (e.g. `fixtures.started`, most of
  `players` and `gw_player_stats`) and keyed `gw_player_stats` on
  `(season_id, player_fpl_id, gw_number)`, which made the job's
  `ON CONFLICT (season_id, player_fpl_id, fixture_fpl_id)` fail.
- **Docs no longer promise "3+ seasons" of history.** The job only loads the
  current season (the FPL API serves nothing older) and does not write the
  `gameweeks` table; README, tool descriptions and schema notes now say so.

- **Ingestion jobs now fail loudly.** `ingest_press_content` and
  `ingest_match_data` previously logged an error and exited 0 when credentials
  were missing or the FPL fetch/DB write failed, so scheduled runs showed green
  while doing nothing. `run()` now returns a bool and the new `main()` CLI
  entry points (used by `sports-context-ingest-press` / `-match`) exit 1 on
  failure, so CI marks the run red and GitHub sends a failure email.
- **Empty env vars fall back to defaults.** `GUARDIAN_API_KEY` and
  `PINECONE_INDEX_NAME` set to an empty string (what GitHub Actions passes for
  an unset secret) previously overrode the `test` / `the-gaffer` defaults.
- **`mcp` dependency pinned to `<2.0.0`** — `server.py` uses the mcp 1.x
  low-level `Server` decorator API (`@server.list_tools()` etc.), which
  mcp 2.x removed. The previous unbounded `mcp>=1.0.0` constraint meant a
  fresh install today would pull mcp 2.2.0 and crash immediately on
  startup.
- **`query_press_conferences` no longer returns a silent empty string**
  when the Pinecone namespace has no matches — it now returns a message
  explaining the namespace may be unseeded or fully aged-out, so this
  doesn't look like a working-but-answerless tool.

## [0.2.0] — 2026-05-11

### Removed

- **API-Sports dependency from `jobs/ingest_match_data.py`** — the supplementary
  PL standings fetch (Thread 2) has been deleted along with the `_sports_get`
  helper, `_SPORTS_BASE` / `_PL_LEAGUE_ID` constants, and the
  `_current_season_start_year` helper. The job now runs a single FPL fetch
  thread plus the delta writer. The Gaffer app no longer consumes API-Sports
  data, so the dependency is no longer needed.

- **`api_sports_key` from `config.py`** and the matching `API_SPORTS_KEY` env
  var from `.github/workflows/ingest_match_data.yml`, `tests/conftest.py`, the
  `server.py --check` output, and the docs.

## [0.1.0]

### Added

- **Package scaffolding** — `sports-context-mcp` structured as a standalone,
  installable Python package (`pyproject.toml`) ready to be extracted into its
  own repository. Dependencies are declared explicitly and do not rely on the
  Gaffer's `requirements.txt`.

- **`config.py`** — Self-contained configuration module that reads from
  environment variables (and a `.env` file if `python-dotenv` is installed).
  Uses the same variable names as the Gaffer server so a single `.env` covers
  both packages during local development.

- **`tools/query_press_conferences.py`** — MCP tool that performs semantic search
  over the Pinecone `press` namespace. Embeds queries with `multilingual-e5-large`
  via Pinecone built-in inference and applies the same recency-weighted re-ranking
  formula used in the Gaffer's `server/rag.py`.

- **`tools/query_historical_stats.py`** — MCP tool that executes read-only SQL
  SELECT statements against the Gaffer PostgreSQL database. Includes a mutation
  keyword blocklist, a 10-second statement timeout, and a 100-row result cap.
  Inline schema description helps LLMs construct valid queries without a separate
  schema-inspection tool call.

- **`server.py`** — MCP server entry point (stdio transport). Registers both tools
  via the `mcp` Python SDK and dispatches incoming tool calls. Can be registered
  in `claude_desktop_config.json` or run directly with `python server.py`.

- **`jobs/ingest_press_content.py`** — Threaded press ingestion job. Fetches
  Premier League content from BBC Sport (RSS) and The Guardian (open content API
  at `content.guardianapis.com`) concurrently via `ThreadPoolExecutor`. Follows
  the exact embedding and upsert pattern from `pipeline/ingest_press.py`:
  `multilingual-e5-large`, batch size 96, `input_type="passage"`, namespace
  `press`. Stale articles (>14 days) are deleted after each run. Adding a new
  source requires only subclassing `_BaseFetcher` and appending to `FETCHERS`.

- **`jobs/ingest_match_data.py`** — Threaded match data ingestion job. Thread 1
  fetches FPL bootstrap + fixtures; Thread 2 fetches API-Sports PL standings.
  `concurrent.futures.wait()` blocks Thread 3 (the delta writer) until both fetch
  threads complete. The delta writer finds the latest `kickoff_time` in PostgreSQL
  and writes only fixtures newer than that timestamp, then fetches and upserts
  `gw_player_stats` for any newly-finished fixtures. Partial fetch failures are
  logged with full tracebacks and do not abort the job.

- **`.github/workflows/ingest_press_content.yml`** — GitHub Actions workflow that
  runs `ingest_press_content` nightly at midnight UTC, on push to the `ingestion`
  branch, and on manual dispatch.

- **`.github/workflows/ingest_match_data.yml`** — GitHub Actions workflow that
  runs `ingest_match_data` twice daily (06:00 + 22:00 UTC) by default, with
  detailed inline comments explaining how to adjust the schedule for FPL
  gameweek cadence, World Cup match cadence, and off-season operation.
