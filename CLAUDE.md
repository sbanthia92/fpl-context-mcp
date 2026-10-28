# fpl-context-mcp

Standalone MCP server that exposes live Fantasy Premier League data (gameweek, fixtures with FDR,
player search and match history, manager squads, expected points, injury/availability) straight
from the public FPL API. No database, no configuration: `uvx fpl-context-mcp` works out of the box.

**Licensing guardrail.** Only serve data from sources whose terms permit use through an AI tool
(currently the FPL API). Do not add a publisher-content source without a licence that allows AI use.

Works with any MCP-compatible host: stdio (default) for local clients (Claude Desktop/Code,
Cursor, VS Code, Gemini CLI, Codex), or `--transport http` (streamable HTTP at `/mcp`) for
URL-only clients such as ChatGPT connectors. Listed in the official MCP Registry as
`io.github.sbanthia92/fpl-context-mcp`.

## Stack
- **Language**: Python 3.11+
- **MCP framework**: `mcp` Python SDK (stdio transport; streamable HTTP via `StreamableHTTPSessionManager` + uvicorn, stateless)
- **HTTP to FPL**: `requests` (sync) run via `asyncio.to_thread` — one shared client, `tools/fpl_client.py`
- **State**: none on disk. In-memory TTL cache only.

## Package structure
```
fpl-context-mcp/
  config.py                        # Env-var config (HTTP transport settings only)
  server.py                        # MCP server entry point: tool registry, prompts, resources, transports
  tools/
    fpl_client.py                  # Shared FPL API client: TTL cache, stale fallback, lookup helpers
    get_current_gameweek.py        # current/next gameweek + deadline
    get_fixtures.py                # fixtures with FDR
    get_fixture_run.py             # fixture-run difficulty / ticker
    search_players.py              # player search/ranking
    get_player_history.py          # a player's matches this season (vs opponent) + past-season totals
    get_gameweek_stats.py          # every player's stats for one gameweek, ranked
    get_xpts.py                    # baseline expected-points ranking
    get_manager_squad.py           # manager squad + chips by team ID
    get_live_availability.py       # live injury/suspension flags
    prompts.py                     # MCP prompts: captain_pick, transfer_plan, chip_timing
    resources.py                   # MCP resource: fpl://glossary
    ratelimit.py                   # per-caller rate limiter for the HTTP transport
  scripts/canary.py                # Health check of a hosted endpoint
  docs/architecture.md             # Layers, request flow, design decisions and trade-offs
  docs/hosting.md                  # Running a public endpoint
  tests/                           # pytest; everything mocked at fpl_client._fetch
  Dockerfile                       # Hosted HTTP server image (non-root, health check)
  pyproject.toml
  CHANGELOG.md
  LICENSE                          # MIT
  .env.example
  .github/workflows/
    ci.yml                         # ruff + pytest on Python 3.11–3.13 for pushes to main and PRs
    publish.yml                    # On v*.*.* tags: PyPI (Trusted Publishing), then GitHub Release (CHANGELOG notes) + MCP Registry (GitHub OIDC)
    canary.yml                     # Every 15 min: check MCP_CANARY_URL (skips if unset)
  glama.json                       # Glama directory ownership claim (maintainers)
  server.json                      # MCP Registry metadata (version rewritten from the tag at publish time)
```

Installed CLI entry point (`[project.scripts]`): `fpl-context-mcp`.

**History:** versions up to 0.10.x had an optional PostgreSQL SQL tool (`query_historical_stats`) and
ingestion jobs. They were removed in 0.11.0: FPL serves match-level data for the current season only
and past seasons as totals, so the database mostly copied what the live API serves. Anyone who needs
the old behaviour pins `fpl-context-mcp<0.11`. Do not reintroduce a database dependency without a
data source that adds something the API doesn't.

## Dev commands
```bash
pip install -e ".[dev]"           # install with dev extras
ruff check . && ruff format .     # lint + format (must pass before every push)
pytest tests/ -v                  # tests (no network)
python server.py                  # stdio; wire into claude_desktop_config.json
python server.py --transport http # http://127.0.0.1:8000/mcp
python server.py --check          # confirm the FPL API is reachable
```

## Configuration

Nothing is required. For the HTTP transport, env vars (or a `.env` file, loaded by `config.py`
when `python-dotenv` is installed):

| Variable | Default | Purpose |
|---|---|---|
| `MCP_AUTH_TOKEN` | (empty) | Require `Authorization: Bearer <token>` on `/mcp`. Comma-separated for several keys. |
| `MCP_TRANSPORT` / `MCP_HOST` / `MCP_PORT` (or `PORT`) | `stdio` / `127.0.0.1` / `8000` | Defaults for `--transport` / `--host` / `--port`. |
| `MCP_RATE_LIMIT_PER_MIN` | `0` (off) | Requests/min per caller (token, else IP) on `/mcp`; over → 429. In-memory, per process. |
| `MCP_CLIENT_IP_HEADER` | (empty) | Header with the real client IP behind a trusted proxy. Never set unless the proxy overwrites it (spoofable otherwise). |

## MCP tools

Every tool is registered once in `server.py` via `_register(Tool, handler)` and carries
`readOnlyHint` annotations. All tools share `tools/fpl_client.py` (one cached `bootstrap-static`
download; per-path TTLs; stale fallback; 256-entry cap). Add new tools on top of it — do not add a
second HTTP client. Tools return plain-text tables, validate their inputs, and resolve names
through `fpl_client.resolve_team` / `fpl_client.find_player` (which list candidates when ambiguous).

| Tool | Source | Notes |
|---|---|---|
| `get_current_gameweek` | bootstrap `events` | current/next GW + deadline |
| `get_fixtures` | `fixtures/` | FDR; `team` flips to that team's point of view; default next 5 GWs |
| `get_fixture_run` | bootstrap + `fixtures/` | avg FDR, home/away, blanks/doubles; no team → ranked ticker |
| `search_players` | bootstrap `elements` | filters name/team/position/max_price; sort whitelist `SORT_FIELDS`; limit ≤ 50 |
| `get_player_history` | `element-summary/{id}/` | `history` = this season's matches (filter by opponent / last_n), `history_past` = season totals. **Current-season match data only** — keep the caveat in output and docs |
| `get_gameweek_stats` | `event/{gw}/live/` | ranked per-gameweek stats; finished GWs cached 1 h, live ones 2 min; labels in-progress GWs |
| `get_xpts` | bootstrap + `fixtures/` | baseline xPts from per-90 rates × expected minutes × availability × FDR; shown beside `ep_next`. Keep the "baseline, noisy early season" caveat in output and docs |
| `get_manager_squad` | `entry/{id}/`, `/event/{gw}/picks/`, `/history/` | public endpoints; 404 → "no data for team" |
| `get_live_availability` | bootstrap `elements` | status/chance/news; optional `players` and `statuses` filters; capped at 100 rows |

### Prompts and resources
- `tools/prompts.py`: `captain_pick`, `transfer_plan`, `chip_timing`. Instruction text only
  (no data, so never stale); arguments optional and validated (numeric ids, chip whitelist).
  If you rename or change a tool, update the tool names inside these prompts.
- `tools/resources.py`: `fpl://glossary` (status codes, FDR, data limits, tool guide). Keep its
  tool guide in sync when tools change.

## HTTP transport and hosting
`build_http_app(auth_token, rate_limit, client_ip_header)` serves `/mcp` (stateless streamable
HTTP, optional bearer keys, per-caller rate limit → 429 + `Retry-After`) and an unauthenticated,
unlimited `/health`. See `docs/hosting.md` for the deployment checklist and `scripts/canary.py` +
`canary.yml` for monitoring. ChatGPT/claude.ai connectors can't send a bearer key, so a public
endpoint for them must run open — the rate limit is the protection.

## Potential next features
Ideas only, not committed to. All fit the guardrails: public FPL API, no database, built on
`tools/fpl_client.py`. Cheapest first.
- **`get_price_changes`**: risers/fallers and likely movers from bootstrap `elements`
  (`cost_change_event`, `transfers_in_event`, `transfers_out_event`, `selected_by_percent`). No extra API calls.
- **`get_set_pieces`**: penalty, corner and free-kick takers per team from bootstrap `elements`
  (`penalties_order`, `corners_and_indirect_freekicks_order`, `direct_freekicks_order`).
- **`get_team_strength`**: home/away attack and defence strength table from bootstrap `teams`
  (`strength_*` fields); pairs with `get_fixture_run`.
- **`get_dream_team`**: team of the week from `dream-team/{gw}/`.
- **`get_league_standings`**: classic league table by league id from `leagues-classic/{id}/standings/`
  (page-capped; one request per page).
- **`get_manager_transfers`**: a manager's transfer history from `entry/{id}/transfers/`; complements `get_manager_squad`.
- **`compare_players`**: side-by-side stats, form, xPts and next fixtures for 2–4 players, composed from existing helpers.
- **Differentials**: `search_players` preset or tool for low-ownership, high-form players.
- **Prompts**: `differential_hunt`, `wildcard_draft` (instruction text only, like the existing three).
- **Hosting**: optional Redis-backed cache and rate limiter for multi-instance deployments; `/metrics` endpoint.
- **Registry polish**: keep Glama, mcprush and MCP Registry listings in sync after each release (descriptions and tool lists go stale).

When one ships, move it into the tool table above and the README, and drop it from this list.

## Git workflow
- **Branch from main**: `git checkout -b fix/description origin/main`
- **PR per change** — keep commits small and descriptive
- **Before pushing**: `ruff check . && ruff format . && pytest tests/ -v`

## Every PR checklist
1. Bump the version in `pyproject.toml`, `server.json` and `CHANGELOG.md`
2. Add a `CHANGELOG.md` entry under the new version
3. Update `CLAUDE.md` and the README if conventions, architecture, tools or env vars change

## Commit conventions
- `feat:` — new tool
- `fix:` — bug fix
- `chore:` — deps, CI, formatting
- `refactor:` — restructure without behaviour change
- `docs:` — documentation only

## Known gotchas
- **MCP Registry ownership marker**: the `<!-- mcp-name: io.github.sbanthia92/fpl-context-mcp -->`
  line at the top of README.md is how the registry verifies the PyPI package. Don't remove it,
  and keep it matching `name` in `server.json`.
- **MCP Registry description limit**: `server.json` `description` must be ≤ 100 characters or the
  registry publish fails with a 422 after PyPI has already published (`tests/test_server_json.py`
  enforces it). A failed registry step can only be fixed by a new version, not a re-run.
- **Tags publish**: pushing a `v*.*.*` tag publishes to PyPI and can't be undone. Only tag from main.
- **HTTP `/mcp` route**: it's a Starlette `Route` with an ASGI class instance, not a `Mount` —
  `Mount` 307-redirects `/mcp` to `/mcp/`, which some clients don't follow.
- **Module-name collision**: the package installs top-level `server.py` and `config.py` modules into
  site-packages. A project with its own `server/` package (the Gaffer) shadows them — never
  `import server` / `import config` expecting the MCP there.
- **Tool-name collisions**: hosts that merge this server's tools with their own (the Gaffer has its
  own `get_fixtures`) must handle duplicate names.
- **FPL API limits**: `element-summary` and `event/{gw}/live` are per-player / per-gameweek fetches;
  they are cached but add FPL traffic on a hosted server. Don't fan out to every player per call.
