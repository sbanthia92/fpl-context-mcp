# fpl-context-mcp

Standalone MCP server that exposes Premier League sports stats and press-conference
RAG as MCP tools, plus threaded ingestion jobs that keep the underlying PostgreSQL
and Pinecone stores up to date.

Works with any MCP-compatible host: stdio (default) for local clients (Claude Desktop/Code,
Cursor, VS Code, Gemini CLI, Codex), or `--transport http` (streamable HTTP at `/mcp`) for
URL-only clients such as ChatGPT connectors. Listed in the official MCP Registry as
`io.github.sbanthia92/fpl-context-mcp`.

## Stack
- **Language**: Python 3.11+
- **MCP framework**: `mcp` Python SDK (stdio transport; streamable HTTP via `StreamableHTTPSessionManager` + uvicorn, stateless)
- **Vector store**: Pinecone (`multilingual-e5-large` built-in inference, namespace `press`)
- **Database**: PostgreSQL (read-only for MCP tools, read/write for ingestion jobs)
- **HTTP**: `requests` (sync, used by jobs) + `asyncpg` (async, used by MCP tools)
- **Concurrency**: `concurrent.futures.ThreadPoolExecutor` in jobs — no asyncio mixing

## Package structure
```
fpl-context-mcp/
  config.py                        # Env-var config
  server.py                        # MCP server entry point (stdio transport)
  tools/
    query_historical_stats.py      # MCP tool: read-only SQL → PostgreSQL
    query_press_conferences.py     # MCP tool: semantic search → Pinecone 'press' namespace
  jobs/
    ingest_press_content.py        # Pinecone updater: BBC RSS + Guardian API, threaded
    ingest_match_data.py           # PostgreSQL updater: FPL, all fixtures + stats delta
    backfill_history.py            # One-time: past-season player totals from FPL history_past
  tests/
    conftest.py
    test_config.py
    test_tools_press.py
    test_tools_stats.py
    test_ingest_press_content.py
    test_ingest_match_data.py
  db/
    schema.sql                     # Reference PostgreSQL schema for standalone provisioning
  pyproject.toml
  CHANGELOG.md
  LICENSE                          # MIT
  .env.example
  .github/workflows/
    ingest_press_content.yml       # Nightly press ingestion (this repo's own data, not customers')
    ingest_match_data.yml          # Configurable match data ingestion (this repo's own data, not customers')
    backfill_history.yml           # Manual (workflow_dispatch) past-season backfill
    publish.yml                    # On v*.*.* tags: PyPI (Trusted Publishing), then MCP Registry (GitHub OIDC)
  server.json                      # MCP Registry metadata (version rewritten from the tag at publish time)
```

Installed CLI entry points (`[project.scripts]` in `pyproject.toml`): `fpl-context-mcp`
(the server), `fpl-context-ingest-press`, `fpl-context-ingest-match` (the two recurring jobs),
`fpl-context-backfill-history` (one-time) — callable directly after `pip install`, no repo clone needed.

**Data ownership model**: this package is bring-your-own-backend. Every install talks to
whatever `DATABASE_URL`/`PINECONE_API_KEY` the operator configures — their own storage,
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
python -m jobs.ingest_press_content
python -m jobs.ingest_match_data
python -m jobs.backfill_history   # one-time, past seasons
```

## Configuration

All config is read from environment variables. A `.env` file at the repo root is
loaded automatically by `config.py` when `python-dotenv` is installed.

| Variable            | Required | Default      | Purpose |
|---------------------|----------|--------------|---------|
| `PINECONE_API_KEY`  | Yes      | —            | Pinecone API key |
| `PINECONE_INDEX_NAME` | No     | `fpl-context` | Pinecone index name |
| `DATABASE_URL`      | Yes*     | —            | Read-only PostgreSQL DSN (`fpl_readonly` user) |
| `DATABASE_ETL_URL`  | Yes*     | —            | Read/write PostgreSQL DSN (`fpl_etl` user). Falls back to `DATABASE_URL`. |
| `GUARDIAN_API_KEY`  | No       | (empty)      | Guardian open platform key. Register free at open-platform.theguardian.com. Without it the Guardian source is skipped (BBC only). |
| `MCP_AUTH_TOKEN`    | No       | (empty)      | HTTP transport only: require `Authorization: Bearer <token>` on `/mcp`. |
| `MCP_TRANSPORT` / `MCP_HOST` / `MCP_PORT` (or `PORT`) | No | `stdio` / `127.0.0.1` / `8000` | Defaults for `--transport` / `--host` / `--port`. |

*Required for the respective tool/job to function; the package will start without them
and log an error on first use.

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

### `query_historical_stats`
Executes a read-only SQL SELECT against the FPL PostgreSQL database.
- Blocks mutation keywords (INSERT/UPDATE/DELETE/DROP etc.)
- 10-second statement timeout
- 100-row result cap
- Inline schema description helps the model write valid queries without a schema-lookup call

### `query_press_conferences`
Semantic search over the Pinecone `press` namespace (BBC Sport + Guardian articles,
FPL player injury updates). Applies recency-weighted re-ranking and returns a
length-capped snippet per document.

## Ingestion jobs

### `ingest_press_content`
- Thread 1: BBC Sport PL RSS → press articles
- Thread 2: Guardian API (`content.guardianapis.com`) → press articles
- Sequential after threads: FPL bootstrap → player injury/availability docs (one doc per
  player, stable ID, overwritten every run; docs for players whose news FPL has cleared are
  deleted after each successful run — skipped if the FPL fetch returned nothing)
- Embeds with `multilingual-e5-large`, batch 96, upserts to `press` namespace
- Deletes articles older than 14 days on every run

**Adding a new source**: subclass `_BaseFetcher`, implement `fetch()`, append to `FETCHERS`.
No other changes needed.

### `ingest_match_data`
- Thread 1: FPL API bootstrap-static + fixtures
- `concurrent.futures.wait([f1])` ensures Thread 3 never starts until the fetch thread completes
- Thread 3 (write, one transaction): upserts season, teams, gameweeks, players and **ALL**
  fixtures every run, then fetches per-player match stats (`element-summary`, once per player,
  8 concurrent requests) only for finished fixtures that have no stats yet or kicked off in the
  last 2 days (bonus points get revised). Batched `execute_values` upsert; rolls back on error.
- Only needs `DATABASE_ETL_URL` (falls back to `DATABASE_URL`).

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

## Pinecone document schema

Documents upserted to the `press` namespace carry this metadata:
```python
{
    "text": str,  # Full document text (embedded)
    "type": "press_article" | "player_news",
    "source": str,  # "BBC Sport" | "The Guardian" | "FPL"
    "date": str,  # RFC 2822 or ISO 8601
    "pub_timestamp": float,  # Unix timestamp — used for stale-doc deletion
    "recency_score": float,  # 1.0 (today) → 0.1 (14 days) — used for re-ranking
    "refreshed_at": float,  # player_news only — run timestamp, used to prune cleared news
    "url": str,  # press_article only
}
```

## Git workflow
- **Branch from main**: `git checkout -b fix/description origin/main`
- **PR per change** — keep commits small and descriptive
- **Before pushing**: `ruff check . && ruff format . && pytest tests/ -v`

## Every PR checklist
1. Bump the version in `pyproject.toml` and `CHANGELOG.md`
2. Add a `CHANGELOG.md` entry under the new version
3. Update `CLAUDE.md` if conventions, architecture, or env vars change

## Commit conventions
- `feat:` — new tool, job, or fetcher
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
- **Guardian API key required**: the public `test` key now returns HTTP 401, so the Guardian
  fetcher is skipped unless `GUARDIAN_API_KEY` is a registered key (free). BBC still ingests.
- **Pinecone inference rate limits**: the 8-second sleep between embed batches in `_upsert`
  exists to avoid HTTP 429s on the free inference tier. Remove or reduce it on paid tiers.
- **Thread 3 ordering**: `concurrent.futures.wait([f1])` is the only enforcement that
  Thread 3 starts after Thread 1 (the FPL fetch). Do not refactor this to `as_completed`
  in a way that allows the writer to start before the fetcher finishes.
- **psycopg2 vs asyncpg**: jobs use `psycopg2` (sync), MCP tools use `asyncpg` (async).
  Do not swap them — jobs must stay sync to avoid mixing asyncio and threading.
