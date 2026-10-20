# fpl-context-mcp

Standalone MCP server that exposes live Fantasy Premier League data (gameweek, fixtures with FDR,
player search, manager squads, injury/availability) straight from the public FPL API with zero
setup, plus an optional read-only SQL tool over a PostgreSQL history database and threaded
ingestion jobs that keep that database up to date.

**Licensing guardrail.** Only ingest or serve data from sources whose terms permit use through
an AI tool (currently the FPL API). Do not add a publisher-content source without a licence that
allows AI use.

Works with any MCP-compatible host: stdio (default) for local clients (Claude Desktop/Code,
Cursor, VS Code, Gemini CLI, Codex), or `--transport http` (streamable HTTP at `/mcp`) for
URL-only clients such as ChatGPT connectors. Listed in the official MCP Registry as
`io.github.sbanthia92/fpl-context-mcp`.

## Stack
- **Language**: Python 3.11+
- **MCP framework**: `mcp` Python SDK (stdio transport; streamable HTTP via `StreamableHTTPSessionManager` + uvicorn, stateless)
- **Database**: PostgreSQL (read-only for MCP tools, read/write for ingestion jobs)
- **HTTP**: `requests` (sync, used by jobs) + `asyncpg` (async, used by MCP tools)
- **Concurrency**: `concurrent.futures.ThreadPoolExecutor` in jobs — no asyncio mixing

## Package structure
```
fpl-context-mcp/
  config.py                        # Env-var config
  server.py                        # MCP server entry point (stdio transport)
  tools/
    fpl_client.py                  # Shared FPL API client: TTL cache, stale fallback, lookup helpers
    query_historical_stats.py      # MCP tool: read-only SQL → PostgreSQL (listed only if DATABASE_URL set)
    get_live_availability.py       # MCP tool: live injury/suspension flags from FPL (no DB)
    get_current_gameweek.py        # MCP tool: current/next gameweek + deadline (no DB)
    get_fixtures.py                # MCP tool: fixtures with FDR (no DB)
    search_players.py              # MCP tool: player search/ranking (no DB)
    get_manager_squad.py           # MCP tool: manager squad + chips by team ID (no DB)
    get_fixture_run.py             # MCP tool: fixture-run difficulty / ticker (no DB)
    get_xpts.py                    # MCP tool: baseline expected-points ranking (no DB)
  jobs/
    ingest_match_data.py           # PostgreSQL updater: FPL, all fixtures + stats delta
    backfill_history.py            # One-time: past-season player totals from FPL history_past
  tests/
    conftest.py
    test_config.py
    test_tools_stats.py
    test_tools_availability.py
    test_tools_live.py
    test_ingest_match_data.py
    test_backfill_history.py
    test_server_http.py
  db/
    schema.sql                     # Reference PostgreSQL schema for standalone provisioning
  pyproject.toml
  CHANGELOG.md
  LICENSE                          # MIT
  .env.example
  .github/workflows/
    ingest_match_data.yml          # Configurable match data ingestion (this repo's own data, not customers')
    backfill_history.yml           # Manual (workflow_dispatch) past-season backfill
    ci.yml                         # ruff + pytest on Python 3.11–3.13 for pushes to main and PRs
    publish.yml                    # On v*.*.* tags: PyPI (Trusted Publishing), then GitHub Release (CHANGELOG notes) + MCP Registry (GitHub OIDC)
  glama.json                       # Glama directory ownership claim (maintainers)
  server.json                      # MCP Registry metadata (version rewritten from the tag at publish time)
```

Installed CLI entry points (`[project.scripts]` in `pyproject.toml`): `fpl-context-mcp`
(the server), `fpl-context-ingest-match` (the recurring job),
`fpl-context-backfill-history` (one-time) — callable directly after `pip install`, no repo clone needed.

**Data ownership model**: this package is bring-your-own-backend. Every install talks to
whatever `DATABASE_URL` the operator configures — their own storage,
empty until they run the ingestion jobs themselves (README: "Seeding data" / "Keeping data
fresh"). The `.github/workflows/ingest_*.yml` files in this repo only run against secrets
configured on `sbanthia92/fpl-context-mcp` and feed the maintainer's own database — they
do not update data on behalf of anyone who installs the package from PyPI.

## Dev commands
```bash
# Install (editable) with dev extras
pip install -e ".[dev]"

# Lint + format (must pass before every push)
ruff check . && ruff format .

# Tests
pytest tests/ -v

# Run the MCP server locally (stdio — wire into claude_desktop_config.json)
python server.py
# ...or over HTTP at http://127.0.0.1:8000/mcp
python server.py --transport http

# Run ingestion jobs manually
python -m jobs.ingest_match_data
python -m jobs.backfill_history   # one-time, past seasons
```

## Configuration

All config is read from environment variables. A `.env` file at the repo root is
loaded automatically by `config.py` when `python-dotenv` is installed.

| Variable            | Required | Default      | Purpose |
|---------------------|----------|--------------|---------|
| `DATABASE_URL`      | No (SQL tool only) | —  | Read-only PostgreSQL DSN (`fpl_readonly` user). Unset → `query_historical_stats` is not listed; the live tools still work. |
| `DATABASE_ETL_URL`  | Yes*     | —            | Read/write PostgreSQL DSN (`fpl_etl` user). Falls back to `DATABASE_URL`. |
| `MCP_AUTH_TOKEN`    | No       | (empty)      | HTTP transport only: require `Authorization: Bearer <token>` on `/mcp`. |
| `MCP_TRANSPORT` / `MCP_HOST` / `MCP_PORT` (or `PORT`) | No | `stdio` / `127.0.0.1` / `8000` | Defaults for `--transport` / `--host` / `--port`. |

The live tools need no configuration. Jobs need their DSN to function; the package starts
without any of them and logs an error on first use of a DB-backed component.

## MCP registration (Claude Desktop)

Add to `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "fpl-context": {
      "command": "python",
      "args": ["/absolute/path/to/fpl-context-mcp/server.py"]
    }
  }
}
```

## MCP tools exposed

Every tool is registered once in `server.py` via `_register(Tool, handler)` and carries
`readOnlyHint` annotations. All live tools share `tools/fpl_client.py` (one cached
`bootstrap-static` download; per-path TTLs; stale fallback; 256-entry cap). Add new live tools
on top of it — do not add a second HTTP client. Live tools return plain-text tables, validate
their inputs, and resolve team names through `fpl_client.resolve_team`.

| Tool | Source | Notes |
|---|---|---|
| `get_current_gameweek` | bootstrap `events` | current/next GW + deadline |
| `get_fixtures` | `fixtures/` | FDR; `team` flips to that team's point of view; default next 5 GWs |
| `search_players` | bootstrap `elements` | filters name/team/position/max_price; sort whitelist `SORT_FIELDS`; limit ≤ 50 |
| `get_fixture_run` | bootstrap + `fixtures/` | avg FDR, home/away, blanks/doubles; no team → ranked ticker |
| `get_xpts` | bootstrap + `fixtures/` | baseline xPts from per-90 rates × expected minutes × availability × FDR; shown beside `ep_next`. Keep the "baseline, noisy early season" caveat in output and docs |
| `get_manager_squad` | `entry/{id}/`, `/event/{gw}/picks/`, `/history/` | public endpoints; 404 → "no data for team" |

### `query_historical_stats`
Listed only when `DATABASE_URL` is set. Executes a read-only SQL SELECT against the FPL PostgreSQL database.
- Blocks mutation keywords (INSERT/UPDATE/DELETE/DROP etc.)
- 10-second statement timeout
- 100-row result cap
- Inline schema description helps the model write valid queries without a schema-lookup call,
  including the availability columns (`status` codes, `chance_of_playing_next_round`, `news`,
  `news_added`). The tool description points the model at `get_live_availability` for current
  flags — keep that line.

### `get_live_availability`
Fetches FPL `bootstrap-static` on demand and returns each player's `status`,
`chance_of_playing_next_round`, `news` and `news_added`. No database, no config.
- Optional `players` (name substrings) and `statuses` (a/d/i/s/u/n) filters; with none, lists
  every flagged player, newest news first, capped at 100 rows
- In-memory cache, 5 min TTL (`_CACHE_TTL_SECONDS`); on a fetch failure serves the stale cache
  with a note, or returns an error pointing at `query_historical_stats` when there is none
- Sync `requests` run via `asyncio.to_thread` — do not add a second HTTP client
- The `query_historical_stats` tool description points the model at this tool for current flags

## Ingestion jobs

### `ingest_match_data`
- Thread 1: FPL API bootstrap-static + fixtures
- `concurrent.futures.wait([f1])` ensures Thread 3 never starts until the fetch thread completes
- Thread 3 (write, one transaction): upserts season, teams, gameweeks, players and **ALL**
  fixtures every run, then fetches per-player match stats (`element-summary`, once per player,
  8 concurrent requests) only for finished fixtures that have no stats yet or kicked off in the
  last 2 days (bonus points get revised). Batched `execute_values` upsert; rolls back on error.
- Only needs `DATABASE_ETL_URL` (falls back to `DATABASE_URL`).
- `players.news_added` (added in 0.7.0): `_ensure_news_added_column` adds it to older databases
  inside a savepoint; without table ownership it logs the `ALTER TABLE` to run and players are
  written without the column (`with_news_added=False`).

**Fetch failure**: if the fetch thread fails, its result is `None` and the writer logs the full
traceback and returns False; the CLI exits 1.

**Do not reintroduce a `MAX(kickoff_time)` delta for fixtures.** The first run stores all 380
fixtures including future ones, so the boundary becomes the season's last day and every later run
writes nothing — results and stats silently freeze. The delta belongs on *stats*, not fixtures.

### `backfill_history` (one-time)
- Reads `/element-summary/{id}/` `history_past` for every player in the current FPL list and
  upserts one `players` row per player per past season (season totals; `team_fpl_id` NULL, `fpl_id`
  = current id). Skips the current season. Best-effort `ALTER TABLE players ALTER COLUMN
  team_fpl_id DROP NOT NULL` for databases created from an older schema.
- Limitation: only players in the current FPL list, so departed players and league-wide past totals
  are incomplete. FPL serves no past fixtures/teams/per-match stats.

## Git workflow
- **Branch from main**: `git checkout -b fix/description origin/main`
- **PR per change** — keep commits small and descriptive
- **Before pushing**: `ruff check . && ruff format . && pytest tests/ -v`

## Every PR checklist
1. Bump the version in `pyproject.toml` and `CHANGELOG.md`
2. Add a `CHANGELOG.md` entry under the new version
3. Update `CLAUDE.md` if conventions, architecture, or env vars change

## Commit conventions
- `feat:` — new tool or job
- `fix:` — bug fix
- `chore:` — deps, CI, formatting
- `refactor:` — restructure without behaviour change
- `docs:` — documentation only

## Known gotchas
- **MCP Registry ownership marker**: the `<!-- mcp-name: io.github.sbanthia92/fpl-context-mcp -->`
  line at the top of README.md is how the registry verifies the PyPI package. Don't remove it,
  and keep it matching `name` in `server.json`.
- **HTTP `/mcp` route**: it's a Starlette `Route` with an ASGI class instance, not a `Mount` —
  `Mount` 307-redirects `/mcp` to `/mcp/`, which some clients don't follow.
- **Thread 3 ordering**: `concurrent.futures.wait([f1])` is the only enforcement that
  Thread 3 starts after Thread 1 (the FPL fetch). Do not refactor this to `as_completed`
  in a way that allows the writer to start before the fetcher finishes.
- **psycopg2 vs asyncpg**: jobs use `psycopg2` (sync), MCP tools use `asyncpg` (async).
  Do not swap them — jobs must stay sync to avoid mixing asyncio and threading.
