# fpl-context-mcp

<!-- mcp-name: io.github.sbanthia92/fpl-context-mcp -->

An [MCP](https://modelcontextprotocol.io) server that gives any MCP-capable AI agent a queryable Fantasy Premier League (FPL) database: player and match stats, fixtures, gameweeks, and every player's injury/availability status. It runs locally in Claude Desktop, Claude Code, Cursor, VS Code Copilot, Windsurf, Gemini CLI and Codex; ChatGPT and other clients that only accept a URL can connect when you [host it over HTTP](#remote-access-over-http-chatgpt-and-other-url-only-clients).

| Tool | What it does |
|---|---|
| `query_historical_stats` | Runs a read-only SQL SELECT against a PostgreSQL database of FPL player, fixture and gameweek stats (whatever seasons you've ingested), including each player's FPL status, chance of playing, and injury/news note |
| `get_live_availability` | Fetches every player's current FPL injury, suspension and availability flags live from FPL — no database or ingestion needed — so a player flagged an hour ago shows up now |

An ingestion job keeps that data populated and current:

| Job | What it does |
|---|---|
| `ingest_match_data` | Fetches teams, fixtures, players (with availability) and per-match player stats from the FPL API, and writes them to PostgreSQL |

**What it doesn't do: press coverage.** Match reports, manager quotes and press-conference news aren't included — publishers' terms don't allow their articles to be stored and served through an AI tool. The tool description tells the model to use its own web search for that, which Claude, ChatGPT and Gemini all have. The division of labour: this server answers "who's injured, who's in form, what are the fixtures"; the AI's web search answers "what did the manager say".

> **`query_historical_stats` only reads what is already in *your* PostgreSQL database.** It starts out **empty** — you must run the ingestion job once to seed it, and then keep running it **on a recurring schedule forever**, or its answers will silently go stale. This is not a one-time setup step. See [Keeping data fresh (ongoing)](#keeping-data-fresh-ongoing). `get_live_availability` is the exception: it needs no database and always returns FPL's current injury and suspension flags.

---

## Contents

- [Quickstart](#quickstart)
- [Prerequisites](#prerequisites)
- [Installation](#installation)
- [Configuration](#configuration)
- [Provisioning your database](#provisioning-your-database)
- [Seeding data (required before first use)](#seeding-data-required-before-first-use)
- [Keeping data fresh (ongoing)](#keeping-data-fresh-ongoing)
- [Registering with Claude Desktop](#registering-with-claude-desktop)
- [Other AI clients (local)](#other-ai-clients-local)
- [Remote access over HTTP (ChatGPT and other URL-only clients)](#remote-access-over-http-chatgpt-and-other-url-only-clients)
- [Running the server standalone](#running-the-server-standalone)
- [Verifying connectivity (--check)](#verifying-connectivity---check)
- [Dry-run mode](#dry-run-mode)
- [MCP tools reference](#mcp-tools-reference)
- [Database schema](#database-schema)
- [Running tests](#running-tests)
- [Data sources and disclaimer](#data-sources-and-disclaimer)
- [License](#license)

---

## Quickstart

The full path from zero to a working MCP tool, in order. Each step links to details further down.

1. **Install**: `pip install fpl-context-mcp` — see [Installation](#installation).
2. **Provision storage**: a PostgreSQL database. Run [`db/schema.sql`](db/schema.sql) against a fresh Postgres database — see [Provisioning your database](#provisioning-your-database).
3. **Configure**: copy [`.env.example`](.env.example) to `.env` and fill in your `DATABASE_URL` and `DATABASE_ETL_URL` — see [Configuration](#configuration).
4. **Verify connectivity**: `fpl-context-mcp --check` — confirms every credential works before you go further.
5. **Seed data**: run the match ingestion command, then the one-time history backfill, so there's actually something to query — see [Seeding data](#seeding-data-required-before-first-use).
6. **Schedule ongoing ingestion**: set up cron (or equivalent) to keep re-running the match ingestion command (not the backfill) indefinitely — see [Keeping data fresh](#keeping-data-fresh-ongoing). Skipping this is the #1 cause of "the tool returns nothing" reports.
7. **Connect your AI client**: [Claude Desktop](#registering-with-claude-desktop), [Claude Code, Cursor, VS Code, Windsurf, Gemini CLI or Codex](#other-ai-clients-local), or — for ChatGPT and other clients that only accept a URL — [run it over HTTP](#remote-access-over-http-chatgpt-and-other-url-only-clients).

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11+ |
| PostgreSQL | Any recent version, with a read-only role (e.g. `fpl_readonly`) and a read/write role (e.g. `fpl_etl`) |

You provision the database yourself — see the next sections. Free tiers (Neon, Supabase, etc.) are plenty: the data is a few MB per season.

---

## Installation

### From PyPI (recommended)

```bash
pip install fpl-context-mcp
```

This installs three CLI commands: `fpl-context-mcp` (the MCP server), `fpl-context-ingest-match` (the recurring ingestion job), and `fpl-context-backfill-history` (a one-time job for past seasons) — see [Seeding data](#seeding-data-required-before-first-use).

### With uv

```bash
git clone https://github.com/sbanthia92/fpl-context-mcp
cd fpl-context-mcp
uv sync
```

### With pip (from source)

```bash
git clone https://github.com/sbanthia92/fpl-context-mcp
cd fpl-context-mcp
pip install -e ".[dev]"
```

### As a dependency of another project

```
fpl-context-mcp @ git+https://github.com/sbanthia92/fpl-context-mcp.git
```

---

## Configuration

The server reads all secrets from environment variables. Copy [`.env.example`](.env.example) to `.env` in your working directory (it's gitignored) and fill in your own values:

```dotenv
# PostgreSQL — read-only connection for the query_historical_stats tool
DATABASE_URL=postgresql://fpl_readonly:password@localhost:5432/fpl

# PostgreSQL — read/write connection for the ingest_match_data job
# Falls back to DATABASE_URL if not set
DATABASE_ETL_URL=postgresql://fpl_etl:password@localhost:5432/fpl

# HTTP transport only (fpl-context-mcp --transport http). Requests to /mcp must
# send "Authorization: Bearer <token>". Leave empty only when bound to localhost.
# MCP_AUTH_TOKEN=
```

### Which variables does each component need?

| Component | Variables required |
|---|---|
| `query_historical_stats` tool | `DATABASE_URL` |
| `get_live_availability` tool | None (outbound HTTPS access to `fantasy.premierleague.com`) |
| `ingest_match_data` job | `DATABASE_ETL_URL` (or `DATABASE_URL`) |

Run `fpl-context-mcp --check` any time to confirm all of the above are set correctly and reachable — see [Verifying connectivity](#verifying-connectivity---check).

---

## Provisioning your database

```bash
createdb fpl   # or whatever database name you'll use in DATABASE_URL
psql fpl -f db/schema.sql
```

[`db/schema.sql`](db/schema.sql) creates the six tables `query_historical_stats` expects (`seasons`, `teams`, `gameweeks`, `players`, `fixtures`, `gw_player_stats`) and includes example `CREATE ROLE` statements for the read-only and read/write roles referenced in `.env.example`. It's a starting schema, not a full migration tool — adjust types/constraints as needed.

The tables start **completely empty**. Continue to [Seeding data](#seeding-data-required-before-first-use).

---

## Seeding data (required before first use)

The ingestion jobs are plain commands you run directly — nothing runs automatically on `pip install` or on MCP server startup.

```bash
# If installed from PyPI
fpl-context-ingest-match
fpl-context-backfill-history   # one-time: past seasons (see below)

# If running from source
python -m jobs.ingest_match_data
python -m jobs.backfill_history
```

Run these **once, right after configuring your `.env`**, before registering the server with Claude Desktop. Run `fpl-context-ingest-match` before the backfill. Until you do, `query_historical_stats` returns `Query returned no results.` for any query, since the tables are empty.

`ingest_match_data` loads the **current season**: every team, gameweek, player and fixture, plus per-player stats for matches already played (the first run can take several minutes mid-season, since it fetches stats player by player). Later runs are quick — see [What each run updates](#what-each-run-updates).

`fpl-context-backfill-history` adds **past seasons** (as far back as FPL has them, about 20). It reads FPL's per-player season history and writes one row per player per season into `players`. It's safe to re-run and only needs to run once, since past seasons don't change. **Know its limits:**

- It holds **season totals only** — points, minutes, goals, assists, clean sheets, cards, bonus. FPL doesn't serve past fixtures, teams or match-by-match stats, so those tables only ever contain the current season.
- Past-season rows have `team_fpl_id` set to NULL (FPL doesn't say which team a player was on), and `fpl_id` is the player's *current* FPL id.
- **It only covers players in FPL's current player list.** Anyone who has left the league (or retired) has no history here, so a question about a departed player returns nothing, and league-wide or team-wide totals for a past season are incomplete. Per-player questions about current players are reliable.

---

## Keeping data fresh (ongoing)

**This is not a one-time step.** Fixtures change weekly, player stats update after every match, and injury/availability news changes daily. If you seed once and never run the job again, a query a month later will hit a database that's **missing every result, stat and injury update since your last run**.

You need something to invoke `fpl-context-ingest-match` on a recurring schedule, indefinitely, for as long as the MCP server is in use. (The backfill is not part of this — run it once.) Pick whichever fits your setup:

### What each run updates

Runs are on a clock, not tied to gameweeks — nothing triggers when a match ends. A result shows up in your database at the first run after FPL marks the fixture finished.

| Job | Each run | Freshness with the default schedule |
|---|---|---|
| `fpl-context-ingest-match` | Rewrites all teams, gameweeks (deadlines, current/next flags), players (points, form, price, and availability: status, chance of playing, news and when it changed) and **all 380 fixtures** (scores, finished flags, reschedules). Fetches per-player match stats for newly finished fixtures, and re-fetches those from the last 2 days because FPL revises bonus points after full time. | Up to about 12 hours behind (runs at 06:00 and 22:00 UTC) |

Run more often on matchdays if you want results sooner — each run takes a minute or two, and steady-state runs make very few requests to FPL.

### Option A — cron (simplest, any Linux/macOS host)

```cron
# Match data: twice daily during the season (06:00 + 22:00 UTC)
0 6,22 * * * /path/to/venv/bin/fpl-context-ingest-match >> /var/log/fpl-context-ingest-match.log 2>&1
```

Adjust the match-data cadence to the calendar:

| Period | Recommended cadence |
|---|---|
| PL season (Aug–May) | Twice daily, `0 6,22 * * *` |
| World Cup / tournament group stage | Hourly, `0 * * * *` |
| World Cup / tournament knockout | Every 6 hours, `0 */6 * * *` |
| Off-season | Once daily, `0 8 * * *` |

### Option B — GitHub Actions in your own private repo (free, no server needed)

Best if you don't have a machine that's always on. You don't fork this project — you create a tiny repo of your own with one file that installs the package from PyPI and runs the ingestion command on a schedule.

1. Create a new **private** GitHub repository (any name).
2. Add this file as `.github/workflows/ingest.yml`:

   ```yaml
   name: Ingest sports data

   on:
     schedule:
       - cron: "0 6,22 * * *" # twice daily, UTC
     workflow_dispatch: {} # lets you run it by hand from the Actions tab

   jobs:
     ingest:
       runs-on: ubuntu-latest
       steps:
         - uses: actions/setup-python@v5
           with:
             python-version: "3.11"
         - run: pip install fpl-context-mcp
         - name: Ingest match data
           run: fpl-context-ingest-match
           env:
             DATABASE_URL: ${{ secrets.DATABASE_URL }}
             DATABASE_ETL_URL: ${{ secrets.DATABASE_ETL_URL }}
   ```

3. In that repo: **Settings → Secrets and variables → Actions → New repository secret**, and add `DATABASE_URL` and `DATABASE_ETL_URL`.
4. Open the **Actions** tab, pick "Ingest sports data", and click **Run workflow** once to seed your data. From then on it runs by itself on the schedule.

Notes:

- **A failed run turns red** and GitHub emails you (missing credentials, a database that's unreachable, an API outage), so you'll know if data stops flowing.
- **Updates:** `pip install fpl-context-mcp` grabs the latest release on every run, so fixes arrive automatically. Pin a version (`fpl-context-mcp==0.3.0`) if you'd rather upgrade on purpose.
- **Cost:** each run takes about a minute or two, so a twice-daily schedule stays well inside GitHub's free monthly minutes for private repos.
- **Why private:** GitHub automatically pauses scheduled workflows in *public* repos after 60 days without a commit. Private repos aren't paused.

### Option C — any other scheduler

Managed cron (Render, Railway, Fly.io machines, GCP Cloud Scheduler + Cloud Run Jobs, AWS EventBridge + Lambda/Fargate, systemd timers, Airflow, Dagster, etc.) all work the same way — point it at `fpl-context-ingest-match` (or `python -m jobs.ingest_match_data`) with the cadence table above and the environment variables from [Configuration](#configuration).

Whichever option you pick, re-run `fpl-context-mcp --check` afterward to confirm the scheduled job's credentials actually work in that environment — a job that silently fails every night is worse than no job, since nothing tells you the data's gone stale.

---

## Registering with Claude Desktop

Add the server to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows).

### If installed from PyPI (recommended)

```json
{
  "mcpServers": {
    "fpl-context": {
      "command": "fpl-context-mcp",
      "env": {
        "DATABASE_URL": "postgresql://fpl_readonly:password@localhost:5432/fpl"
      }
    }
  }
}
```

### If running from source

```json
{
  "mcpServers": {
    "fpl-context": {
      "command": "python",
      "args": ["/absolute/path/to/fpl-context-mcp/server.py"],
      "env": {
        "DATABASE_URL": "postgresql://fpl_readonly:password@localhost:5432/fpl"
      }
    }
  }
}
```

> **Tip:** If you use `uv`, replace `"python"` with `"uv"` and prepend `"run"` to `args`:
> ```json
> "command": "uv",
> "args": ["run", "/absolute/path/to/fpl-context-mcp/server.py"]
> ```

Restart Claude Desktop. You should see `fpl-context` appear in the tools panel. If the tool returns nothing useful, re-check [Seeding data](#seeding-data-required-before-first-use) and [Keeping data fresh](#keeping-data-fresh-ongoing) before assuming the server itself is broken.

---

## Other AI clients (local)

Any client that can launch a local MCP server (stdio) works the same way: run the `fpl-context-mcp` command with `DATABASE_URL` in its environment. Swap in your own values below.

**Claude Code**

```bash
claude mcp add fpl-context -e DATABASE_URL=postgresql://fpl_readonly:password@localhost:5432/fpl -- fpl-context-mcp
```

**Cursor** (`~/.cursor/mcp.json`), **Windsurf** (`~/.codeium/windsurf/mcp_config.json`) and **Gemini CLI** (`~/.gemini/settings.json`) all use the same `mcpServers` shape as Claude Desktop:

```json
{
  "mcpServers": {
    "fpl-context": {
      "command": "fpl-context-mcp",
      "env": {
        "DATABASE_URL": "postgresql://fpl_readonly:password@localhost:5432/fpl"
      }
    }
  }
}
```

**VS Code (Copilot agent mode)** — `.vscode/mcp.json` in your workspace:

```json
{
  "servers": {
    "fpl-context": {
      "type": "stdio",
      "command": "fpl-context-mcp",
      "env": {
        "DATABASE_URL": "postgresql://fpl_readonly:password@localhost:5432/fpl"
      }
    }
  }
}
```

**OpenAI Codex CLI** — `~/.codex/config.toml`:

```toml
[mcp_servers.fpl-context]
command = "fpl-context-mcp"
env = { DATABASE_URL = "postgresql://fpl_readonly:password@localhost:5432/fpl" }
```

**Without installing first** — if you have [uv](https://docs.astral.sh/uv/), use `"command": "uvx"` with `"args": ["fpl-context-mcp"]` in any of the configs above.

**Your own agent code** — the MCP SDKs (Python, TypeScript) and agent frameworks such as the OpenAI Agents SDK can launch `fpl-context-mcp` as a stdio server, or connect to it over HTTP as below.

---

## Remote access over HTTP (ChatGPT and other URL-only clients)

Some clients can't launch a local process — they only accept a server URL. That includes **ChatGPT** (Settings → Apps & Connectors → Advanced → Developer mode → create a connector) and **custom connectors on claude.ai**. For these, run the server with the streamable HTTP transport on a machine with a public HTTPS address:

```bash
MCP_AUTH_TOKEN=some-long-random-string \
fpl-context-mcp --transport http --host 0.0.0.0 --port 8000
```

- The MCP endpoint is `https://<your-host>/mcp`; `GET /health` returns `ok` for load-balancer and platform health checks.
- `--transport`, `--host` and `--port` can also be set with `MCP_TRANSPORT`, `MCP_HOST` and `MCP_PORT` (or the `PORT` variable that Render, Cloud Run, Heroku and Fly set).
- The server listens on plain HTTP. Put it behind something that terminates TLS — any of those platforms does, or `cloudflared tunnel` / `ngrok` for a quick test from your own machine.
- The default bind address is `127.0.0.1`, so nothing is exposed until you pass `--host 0.0.0.0`.

**Authentication.** With `MCP_AUTH_TOKEN` set, every request to `/mcp` must send `Authorization: Bearer <token>`; others get HTTP 401. Clients that let you set headers can use it — for example Claude Code:

```bash
claude mcp add --transport http fpl-context https://your-host/mcp --header "Authorization: Bearer some-long-random-string"
```

and the OpenAI Agents SDK / Responses API MCP tool (`headers={"Authorization": "Bearer ..."}`).

> **ChatGPT and claude.ai connectors only support OAuth or no authentication — not a static bearer token.** To use them you currently have to leave `MCP_AUTH_TOKEN` unset (the endpoint is then open to anyone who finds the URL) or put an OAuth-capable proxy in front. If you run it open, understand what that exposes: anyone can run read-only `SELECT`s against the database behind `DATABASE_URL` (10-second timeout, 100-row cap). Only do that with the dedicated `fpl_readonly` role on a database that holds nothing but FPL data. The server logs a warning at startup when it's bound to a non-local address without a token.

**Where the data comes from.** A hosted server reads *your* database, exactly like a local one — you still need the ingestion job on a schedule ([Keeping data fresh](#keeping-data-fresh-ongoing)). And because you're now serving results to other people, see [Data sources and disclaimer](#data-sources-and-disclaimer).

---

## Running the server standalone

```bash
# If installed from PyPI
fpl-context-mcp

# If running from source
python server.py
```

By default the server communicates over stdio — it is designed to be launched by an MCP client, and running it directly is mainly useful for smoke-testing startup and environment variable loading. To run it as a persistent network service instead, use `--transport http` (see [Remote access over HTTP](#remote-access-over-http-chatgpt-and-other-url-only-clients)).

---

## Verifying connectivity (--check)

Before registering the server with a client — and any time something seems off — verify that your environment variables are correct and all backends are reachable:

```bash
# If installed from PyPI
fpl-context-mcp --check

# If running from source
python server.py --check
```

Output example:

```
=== fpl-context-mcp configuration check ===

✅ PostgreSQL (RO)   connected (localhost:5432/fpl)
✅ PostgreSQL (ETL)  connected (localhost:5432/fpl)

✅ All required components OK
```

The command exits with code `0` if all required components pass, or `1` if any required component fails. Note that `--check` only verifies *connectivity* — it doesn't tell you whether your tables actually have data in them; for that, see [Seeding data](#seeding-data-required-before-first-use).

---

## Dry-run mode

Set `DRY_RUN=true` to fetch data and verify routing without writing anything to PostgreSQL:

```bash
DRY_RUN=true fpl-context-mcp
DRY_RUN=true fpl-context-ingest-match
```

In dry-run mode:

- **Tools** return a human-readable description of the call that *would* have been made — the SQL and database host — without opening any connection.
- **Ingestion jobs** still call all external APIs (verifying connectivity) but skip every PostgreSQL write. Log output shows how many fixtures and players would have been written.
- The server logs a `DRY RUN MODE` warning at startup so it is obvious from the logs.

Accepted values for `DRY_RUN`: `true`, `1`, `yes` (case-insensitive). Any other value (or absent) disables dry-run.

---

## MCP tools reference

### `query_historical_stats`

Executes a read-only SQL `SELECT` against the historical stats database.

**Parameters**

| Parameter | Type | Description |
|---|---|---|
| `sql` | string | A `SELECT` statement. Mutations are rejected before reaching the database. `LIMIT` is injected automatically if omitted (capped at 100 rows). |

**Example prompts**

- *"Who are the top 10 midfielders by total points this season?"*
- *"Which players have scored the most goals this season?"*
- *"Show my captain candidate's goals and points over the last five seasons."* (past seasons only cover players still in the current FPL list)
- *"When is the next gameweek deadline?"*
- *"Which teams have the best defensive record at home this season?"*
- *"Who is injured or doubtful for Arsenal, and what's their chance of playing?"*
- *"Which players had new injury news in the last 3 days?"*

**Safety**

The tool enforces two layers of protection: a keyword blocklist rejects `INSERT`, `UPDATE`, `DELETE`, `DROP`, and similar statements before any database call is made, and the database connection uses a read-only role with no write grants.

### `get_live_availability`

Current FPL injury, suspension and availability flags, fetched live from the FPL API. Use it before recommending or captaining a player, so a newly flagged player isn't missed. It needs no database and no ingestion run.

**Parameters** (all optional)

| Parameter | Type | Description |
|---|---|---|
| `players` | array of strings | Names to look up, e.g. `["Saka", "Haaland"]`. Case-insensitive; matches first, second or web name. Matching players are returned even when fully available. |
| `statuses` | array of strings | Only return players with these FPL status codes: `a` available, `d` doubtful, `i` injured, `s` suspended, `u` unavailable, `n` not in squad. |

With no arguments it returns every player who currently has a flag (any status other than `a`, or a news note), newest news first, capped at 100 rows. Each row has the player, team, position, status, FPL's chance of playing next round, the news note and when it was added.

**Example prompts**

- *"Is Saka fit for this gameweek?"*
- *"Who has been suspended or ruled out in the last few days?"*
- *"Before I set my captain, check that Haaland and Salah have no flags."*

**How it behaves**

- One request to FPL's `bootstrap-static` endpoint returns every player, and the response is cached in memory for 5 minutes, so heavy use costs FPL only a few calls an hour per server process.
- If FPL can't be reached, the last cached copy is served and labelled as possibly out of date. With no cached copy it returns an error pointing at `query_historical_stats`, which has availability as of the last ingestion run.

---

## Database schema

The `query_historical_stats` tool has access to these tables (see [`db/schema.sql`](db/schema.sql) for the full DDL if provisioning standalone):

```
seasons          id, label (e.g. '2025/26'), start_year, is_current

teams            season_id, fpl_id, name, short_name, strength,
                 strength_attack_home/away, strength_defence_home/away

gameweeks        season_id, gw_number (1–38), deadline_time, is_current,
                 is_next, is_finished, average_entry_score, highest_score

players          season_id, fpl_id, team_fpl_id, first_name, second_name,
                 web_name, position (GKP/DEF/MID/FWD), now_cost, form,
                 total_points, minutes, goals_scored, assists, clean_sheets,
                 expected_goals, expected_assists, ict_index, selected_by_percent,
                 status, chance_of_playing_next_round, news, news_added

fixtures         season_id, fpl_id, gw_number, kickoff_time,
                 home_team_fpl_id, away_team_fpl_id, home_score, away_score,
                 finished, home_team_difficulty, away_team_difficulty

gw_player_stats  season_id, player_fpl_id, gw_number, fixture_fpl_id,
                 opponent_team_fpl_id, was_home, minutes, goals_scored,
                 assists, clean_sheets, bonus, total_points,
                 expected_goals, expected_assists, ict_index, starts
```

**Current vs past seasons:** `teams`, `gameweeks`, `fixtures` and `gw_player_stats` hold the **current season only**. `players` also holds one totals-only row per player per past season (see [Seeding data](#seeding-data-required-before-first-use) for what that covers and what it misses).

**Availability:** `status` is `a` available, `d` doubtful, `i` injured, `s` suspended, `u` unavailable, `n` not in squad. `chance_of_playing_next_round` is 0–100 (NULL means no concern), `news` is FPL's one-line note, and `news_added` is when that note last changed. Databases created before 0.7.0 get the `news_added` column added automatically on the next ingestion run (if the ETL role owns the table; otherwise the job logs the one-line `ALTER TABLE` to run).

**Join hint:** `teams.fpl_id = players.team_fpl_id` (current season, same `season_id`; `team_fpl_id` is NULL for past seasons).

---

## Running tests

```bash
# Install dev dependencies if you haven't already
pip install -e ".[dev]"

# Run the full suite (all mocked — no real DB or API calls)
pytest tests/ -v

# Lint and format
ruff check . && ruff format .
```

The test suite covers:

| File | What's tested |
|---|---|
| `tests/test_config.py` | Env var reading, defaults, dotenv loading, dry-run flag |
| `tests/test_tools_stats.py` | Mutation guard, row formatter, async DB path, dry-run |
| `tests/test_ingest_match_data.py` | Fixture/gameweek/player upserts, `news_added` migration, stats selection, thread coordination, rollback, dry-run |
| `tests/test_server_http.py` | HTTP transport: bearer-token auth, `/health`, no `/mcp` redirect, CLI/env argument parsing |
| `tests/test_backfill_history.py` | Past-season backfill: season handling, NULL team, best-effort ALTER, exit codes |

---

## Data sources and disclaimer

fpl-context-mcp is an independent open-source project. It is **not affiliated with, endorsed by, or sponsored by** the Premier League or Fantasy Premier League.

The package ships no data. The ingestion jobs fetch it, on your machine and under your credentials, from:

| Source | Used for | Notes |
|---|---|---|
| Fantasy Premier League API (`fantasy.premierleague.com/api`) | Players, teams, fixtures, match stats, injury/availability news (ingested into PostgreSQL, and fetched live by `get_live_availability`) | Unofficial and undocumented; it can change or rate-limit without notice. |

**No news articles.** The server doesn't store or serve press coverage. For match reports, quotes and press-conference news, let your AI client use its own web search.

**You are responsible for complying with each source's terms of use** for the data you ingest, store, and — if you [host the server](#remote-access-over-http-chatgpt-and-other-url-only-clients) for other people — serve. This is especially relevant for commercial use and for public deployments. The MIT license below covers this project's code only, not any third-party content it retrieves.

---

## License

[MIT](LICENSE) © 2026 Shubham Banthia
