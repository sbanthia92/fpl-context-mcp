# Changelog

All notable changes to fpl-context-mcp (formerly sports-context-mcp) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.6.0] — 2026-09-25

### Added

- **HTTP transport for URL-only clients.** `fpl-context-mcp --transport http`
  serves MCP over streamable HTTP at `/mcp` (plus an unauthenticated `/health`),
  so ChatGPT connectors, claude.ai custom connectors and other clients that can't
  launch a local process can use the server. Stateless, so it can run behind a
  load balancer. `--host` (default `127.0.0.1`) and `--port` (default `8000`) can
  also come from `MCP_HOST` / `MCP_PORT` / `PORT`, and `--transport` from
  `MCP_TRANSPORT`. stdio remains the default.
- **`MCP_AUTH_TOKEN`.** When set, HTTP requests to `/mcp` need
  `Authorization: Bearer <token>`. The server warns at startup when bound to a
  non-local address without one.
- **MCP Registry listing.** `server.json` describes the server for the official
  MCP Registry, the README carries the `mcp-name` ownership marker, and the
  publish workflow lists each tagged release there after it reaches PyPI.
- **README: setup for other AI clients** (Claude Code, Cursor, VS Code, Windsurf,
  Gemini CLI, Codex CLI, ChatGPT), a **data sources and disclaimer** section
  (no affiliation with the Premier League, FPL, BBC or Guardian; users must
  follow each source's terms), and a License section.

### Changed

- The server now reports its own package version in `serverInfo` instead of the
  MCP SDK's version.
- Minimum `mcp` version raised to 1.8.0 (first release with streamable HTTP).

## [0.5.1] — 2026-09-25

### Fixed

- **Old press articles were never deleted.** The 14-day cleanup used a metadata
  filter on `pub_timestamp`, which silently skipped articles stored without that
  field, so articles from months ago stayed in the index and showed up in
  answers. Cleanup now scans every document in the namespace and, for articles
  lacking `pub_timestamp`, dates them from their `date` field; anything older
  than 14 days (or with no usable date) is deleted. Player-news pruning uses the
  same scan. Unknown document types are left alone. The scan needs a serverless
  Pinecone index (`Index.list`).
- **Search results no longer return whole articles.** Each result's text is now
  cut to about 1,500 characters at a word boundary, and the article URL is
  appended when the document has one, so answers are faster and cheaper.

### Changed

- **Removed all references to the predecessor project.** Tool descriptions and docs
  now say "FPL". The default Pinecone index name is now `fpl-context`, and the
  example database name and roles are `fpl`, `fpl_readonly` and `fpl_etl`. If your
  index has a different name, set `PINECONE_INDEX_NAME` explicitly (the README
  has always shown this); setups that already do are unaffected.

## [0.5.0] — 2026-09-25

### Fixed

- **Match results froze after the first run.** `ingest_match_data` only wrote
  fixtures newer than `MAX(kickoff_time)`, but the first run stores all 380
  fixtures including future ones, so the boundary became the last day of the
  season and every later run wrote 0 fixtures — scores, `finished` flags and
  per-player stats never updated. All fixtures are now upserted every run, and
  the delta applies to player stats instead: they are fetched only for finished
  fixtures that have none yet or were played in the last 2 days (FPL revises
  bonus points after full time). Each player's `element-summary` is fetched once
  (8 concurrent requests) and rows are written with one batched upsert, so a first
  seed makes far fewer requests. The job no longer needs the read-only connection.
- **`gameweeks` is now populated** (deadlines, current/next/finished flags,
  average and highest scores) from bootstrap-static on every run; previously the
  table was advertised but never written.

### Added

- **`fpl-context-backfill-history`** (`jobs/backfill_history.py`) — one-time job that
  loads past-season player totals (about 20 seasons) from FPL's `history_past`, plus
  a manual `backfill_history.yml` workflow. Past-season rows have a NULL
  `team_fpl_id`; only players in the current FPL list are covered (FPL serves no
  past fixtures, teams or per-match stats).

### Changed

- **Removed the `player_xpts` view** from `db/schema.sql`, the tool description and
  the docs. It was a projection view that nothing in this project computes, so it
  could only ever hold placeholder values. Existing databases can drop it with
  `DROP MATERIALIZED VIEW IF EXISTS player_xpts;`.
- `players.team_fpl_id` is nullable in `db/schema.sql`. Existing databases:
  `ALTER TABLE players ALTER COLUMN team_fpl_id DROP NOT NULL;` (the backfill tries
  this itself when run as the table owner).
- README documents what each run updates and how fresh the data is, and the tool
  description now states that past seasons are totals-only and current-list-only.

## [0.4.0] — 2026-09-25

### Changed

- **Renamed `sports-context-mcp` to `fpl-context-mcp`** before the first PyPI
  release. The GitHub repo, PyPI package and CLI commands now all say "fpl":
  `fpl-context-mcp` (server), `fpl-context-ingest-press` and
  `fpl-context-ingest-match` (jobs). The MCP server is registered as
  `fpl-context` in the README's Claude Desktop config. Tool names
  (`query_historical_stats`, `query_press_conferences`) and Python module names
  are unchanged. GitHub redirects the old repo URL. Entries below refer to the
  project by its old name.

## [0.3.1] — 2026-09-25

### Fixed

- **Guardian `test` key no longer works.** The Guardian API now answers the
  public `test` key with HTTP 401, so the old default silently produced zero
  Guardian articles. `GUARDIAN_API_KEY` now defaults to empty; without a key the
  Guardian fetcher logs a clear warning and is skipped (BBC Sport still
  ingests), and `--check` says so. README, `.env.example` and `CLAUDE.md`
  updated — register a free key at open-platform.theguardian.com.
- **The Guardian fetcher no longer logs the API key** at INFO level.
- **Outdated injury news is now removed.** Player-news docs used a
  hash-of-text ID and were exempt from cleanup, so a changed or cleared injury
  left the old doc in Pinecone forever. Each player now has one doc with a
  stable ID that is overwritten every run, stamped with `refreshed_at`; after a
  successful run, player-news docs that were not refreshed (news cleared by FPL,
  or written by an older version) are deleted. Cleanup is skipped if the FPL
  fetch returned nothing, so an API outage cannot wipe injury data. The first
  run after upgrading removes all old-format player-news docs once (current
  injuries are recreated in the same run).
- **Two Guardian tests no longer fail by date.** They hardcoded an article date
  that aged past the fetcher's 14-day window; they now use the current time.

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
  server.
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
  an unset secret) previously overrode the `test` / default-index-name defaults.
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
  thread plus the delta writer. Nothing consumes API-Sports data any
  more, so the dependency is no longer needed.

- **`api_sports_key` from `config.py`** and the matching `API_SPORTS_KEY` env
  var from `.github/workflows/ingest_match_data.yml`, `tests/conftest.py`, the
  `server.py --check` output, and the docs.

## [0.1.0]

### Added

- **Package scaffolding** — `sports-context-mcp` structured as a standalone,
  installable Python package (`pyproject.toml`) ready to be extracted into its
  own repository. Dependencies are declared explicitly and do not rely on any
  external requirements file.

- **`config.py`** — Self-contained configuration module that reads from
  environment variables (and a `.env` file if `python-dotenv` is installed).
  Local development picks up a `.env` file automatically.

- **`tools/query_press_conferences.py`** — MCP tool that performs semantic search
  over the Pinecone `press` namespace. Embeds queries with `multilingual-e5-large`
  via Pinecone built-in inference and applies a recency-weighted re-ranking
  formula.

- **`tools/query_historical_stats.py`** — MCP tool that executes read-only SQL
  SELECT statements against the FPL PostgreSQL database. Includes a mutation
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
