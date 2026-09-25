# fpl-context-mcp

An [MCP](https://modelcontextprotocol.io) server that gives Claude (or any MCP client) two tools for answering Fantasy Premier League (FPL) and Premier League football questions:

| Tool | What it does |
|---|---|
| `query_historical_stats` | Runs a read-only SQL SELECT against a PostgreSQL database of FPL player, fixture and gameweek stats (whatever seasons you've ingested) |
| `query_press_conferences` | Semantic search over BBC Sport and The Guardian press-conference summaries and injury updates stored in Pinecone |

Two ingestion jobs keep that data populated and current:

| Job | What it does |
|---|---|
| `ingest_press_content` | Fetches articles from BBC Sport RSS and The Guardian API, embeds them, and upserts into Pinecone |
| `ingest_match_data` | Fetches fixture and player-stat data from the FPL API, and delta-writes to PostgreSQL |

> **This server does not fetch live data per-question.** The two tools above only read whatever is already sitting in *your* PostgreSQL database and Pinecone index. Those stores start out **empty** — you must run the ingestion jobs once to seed them, and then keep running them **on a recurring schedule forever**, or answers will silently go stale (press results) or stay empty (stats results). This is not a one-time setup step. See [Keeping data fresh (ongoing)](#keeping-data-fresh-ongoing) — it's the single most important thing to get right before handing this to anyone.

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
- [Running the server standalone](#running-the-server-standalone)
- [Verifying connectivity (--check)](#verifying-connectivity---check)
- [Dry-run mode](#dry-run-mode)
- [MCP tools reference](#mcp-tools-reference)
- [Database schema](#database-schema)
- [Running tests](#running-tests)
- [Extending with new press sources](#extending-with-new-press-sources)

---

## Quickstart

The full path from zero to a working MCP tool, in order. Each step links to details further down.

1. **Install**: `pip install fpl-context-mcp` — see [Installation](#installation).
2. **Provision storage**: a PostgreSQL database and a Pinecone index. Run [`db/schema.sql`](db/schema.sql) against a fresh Postgres database and create a Pinecone index named `fpl-context` (or your own name) using the `multilingual-e5-large` model — see [Provisioning your own database](#provisioning-your-database).
3. **Configure**: copy [`.env.example`](.env.example) to `.env` and fill in your `DATABASE_URL`, `DATABASE_ETL_URL`, and `PINECONE_API_KEY` — see [Configuration](#configuration).
4. **Verify connectivity**: `fpl-context-mcp --check` — confirms every credential works before you go further.
5. **Seed data**: run the two ingestion commands, then the one-time history backfill, so there's actually something to query — see [Seeding data](#seeding-data-required-before-first-use).
6. **Schedule ongoing ingestion**: set up cron (or equivalent) to keep re-running the two ingestion commands (not the backfill) indefinitely — see [Keeping data fresh](#keeping-data-fresh-ongoing). Skipping this is the #1 cause of "the tool returns nothing" reports.
7. **Register with Claude Desktop**: add the server to `claude_desktop_config.json` and restart Claude — see [Registering with Claude Desktop](#registering-with-claude-desktop).

---

## Prerequisites

| Requirement | Version |
|---|---|
| Python | 3.11+ |
| PostgreSQL | Any recent version, with a read-only role (e.g. `fpl_readonly`) and a read/write role (e.g. `fpl_etl`) |
| Pinecone | An index using the `multilingual-e5-large` model (1024 dims) — free tier works |

You provision both yourself — see the next two sections. Both have free tiers that are enough for this.

---

## Installation

### From PyPI (recommended)

```bash
pip install fpl-context-mcp
```

This installs four CLI commands: `fpl-context-mcp` (the MCP server), `fpl-context-ingest-press` and `fpl-context-ingest-match` (the two recurring ingestion jobs), and `fpl-context-backfill-history` (a one-time job for past seasons) — see [Seeding data](#seeding-data-required-before-first-use).

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

# Pinecone — required for both the press tool and the ingest_press_content job
PINECONE_API_KEY=pcsk_...
PINECONE_INDEX_NAME=fpl-context   # optional, defaults to 'fpl-context'

# The Guardian open platform API key
# Register free at https://open-platform.theguardian.com/access/
# Recommended: without a key the Guardian source is skipped (BBC Sport only) —
# the old public 'test' key is rejected by the API.
GUARDIAN_API_KEY=your-key-here
```

### Which variables does each component need?

| Component | Variables required |
|---|---|
| `query_historical_stats` tool | `DATABASE_URL` |
| `query_press_conferences` tool | `PINECONE_API_KEY` |
| `ingest_press_content` job | `PINECONE_API_KEY` (plus `GUARDIAN_API_KEY` for Guardian articles) |
| `ingest_match_data` job | `DATABASE_ETL_URL` (or `DATABASE_URL`) |

Run `fpl-context-mcp --check` any time to confirm all of the above are set correctly and reachable — see [Verifying connectivity](#verifying-connectivity---check).

---

## Provisioning your database

**PostgreSQL:**

```bash
createdb fpl   # or whatever database name you'll use in DATABASE_URL
psql fpl -f db/schema.sql
```

[`db/schema.sql`](db/schema.sql) creates the six tables `query_historical_stats` expects (`seasons`, `teams`, `gameweeks`, `players`, `fixtures`, `gw_player_stats`) and includes example `CREATE ROLE` statements for the read-only and read/write roles referenced in `.env.example`. It's a starting schema, not a full migration tool — adjust types/constraints as needed.

**Pinecone:**

1. Create a free account at [pinecone.io](https://www.pinecone.io/) if you don't have one.
2. Create an index named `fpl-context` (or any name — just set `PINECONE_INDEX_NAME` to match) configured for the `multilingual-e5-large` **integrated embedding model** (1024 dimensions, cosine metric). No separate embedding step needed — the ingestion job and the query tool both call Pinecone's built-in inference.
3. Grab an API key from the Pinecone console and set `PINECONE_API_KEY`.

Both tables and the index start **completely empty**. Continue to [Seeding data](#seeding-data-required-before-first-use).

---

## Seeding data (required before first use)

Both ingestion jobs are plain functions you run directly — nothing runs automatically on `pip install` or on MCP server startup.

```bash
# If installed from PyPI
fpl-context-ingest-press
fpl-context-ingest-match
fpl-context-backfill-history   # one-time: past seasons (see below)

# If running from source
python -m jobs.ingest_press_content
python -m jobs.ingest_match_data
python -m jobs.backfill_history
```

Run these **once, right after configuring your `.env`**, before registering the server with Claude Desktop. Run `fpl-context-ingest-match` before the backfill. Until you do:

- `query_press_conferences` will return a message telling you the namespace is unseeded, instead of any article content.
- `query_historical_stats` will return `Query returned no results.` for any query, since the tables are empty.

`ingest_match_data` loads the **current season**: every team, gameweek, player and fixture, plus per-player stats for matches already played (the first run can take several minutes mid-season, since it fetches stats player by player). Later runs are quick — see [What each run updates](#what-each-run-updates).

`fpl-context-backfill-history` adds **past seasons** (as far back as FPL has them, about 20). It reads FPL's per-player season history and writes one row per player per season into `players`. It's safe to re-run and only needs to run once, since past seasons don't change. **Know its limits:**

- It holds **season totals only** — points, minutes, goals, assists, clean sheets, cards, bonus. FPL doesn't serve past fixtures, teams or match-by-match stats, so those tables only ever contain the current season.
- Past-season rows have `team_fpl_id` set to NULL (FPL doesn't say which team a player was on), and `fpl_id` is the player's *current* FPL id.
- **It only covers players in FPL's current player list.** Anyone who has left the league (or retired) has no history here, so a question about a departed player returns nothing, and league-wide or team-wide totals for a past season are incomplete. Per-player questions about current players are reliable.

`ingest_press_content` only pulls currently-live articles (BBC/Guardian don't offer deep history), so the press index will be thin until it's had a few days of scheduled runs — that's expected, not a bug.

---

## Keeping data fresh (ongoing)

**This is not a one-time step.** Fixtures change weekly, player stats update after every match, press articles are deleted from the index after 14 days, and injury/availability news is rewritten on every run so it reflects what FPL currently says (`ingest_press_content` prunes stale docs each time). If you seed once and never run these jobs again, a query a month later will hit a Pinecone namespace with **zero documents** (everything aged out) and a Postgres database that's **missing every fixture since your last run**.

You need something to invoke `fpl-context-ingest-press` and `fpl-context-ingest-match` on a recurring schedule, indefinitely, for as long as the MCP server is in use. (The backfill is not part of this — run it once.) Pick whichever fits your setup:

### What each run updates

Runs are on a clock, not tied to gameweeks — nothing triggers when a match ends. A result shows up in your database at the first run after FPL marks the fixture finished.

| Job | Each run | Freshness with the default schedule |
|---|---|---|
| `fpl-context-ingest-match` | Rewrites all teams, gameweeks (deadlines, current/next flags), players (points, form, price, availability) and **all 380 fixtures** (scores, finished flags, reschedules). Fetches per-player match stats for newly finished fixtures, and re-fetches those from the last 2 days because FPL revises bonus points after full time. | Up to about 12 hours behind (runs at 06:00 and 22:00 UTC) |
| `fpl-context-ingest-press` | Adds new BBC/Guardian articles, rewrites every player's injury/availability item with FPL's current text, and deletes articles older than 14 days and injury items FPL has cleared. | Up to about 24 hours behind (nightly) |

Run more often on matchdays if you want results sooner — each run takes a minute or two, and steady-state runs make very few requests to FPL.

### Option A — cron (simplest, any Linux/macOS host)

```cron
# Press content: nightly at midnight UTC
0 0 * * * /path/to/venv/bin/fpl-context-ingest-press >> /var/log/fpl-context-ingest-press.log 2>&1

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

Best if you don't have a machine that's always on. You don't fork this project — you create a tiny repo of your own with one file that installs the package from PyPI and runs the two commands on a schedule.

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
         - name: Ingest press content
           run: fpl-context-ingest-press
           env:
             PINECONE_API_KEY: ${{ secrets.PINECONE_API_KEY }}
             PINECONE_INDEX_NAME: ${{ secrets.PINECONE_INDEX_NAME }}
             GUARDIAN_API_KEY: ${{ secrets.GUARDIAN_API_KEY }}
         - name: Ingest match data
           run: fpl-context-ingest-match
           env:
             DATABASE_URL: ${{ secrets.DATABASE_URL }}
             DATABASE_ETL_URL: ${{ secrets.DATABASE_ETL_URL }}
   ```

3. In that repo: **Settings → Secrets and variables → Actions → New repository secret**, and add `PINECONE_API_KEY`, `DATABASE_URL`, and `DATABASE_ETL_URL`. `GUARDIAN_API_KEY` is strongly recommended — without it the Guardian source is skipped and only BBC Sport articles are ingested (register a free key at [open-platform.theguardian.com](https://open-platform.theguardian.com/access/)). `PINECONE_INDEX_NAME` is optional and defaults to `fpl-context`.
4. Open the **Actions** tab, pick "Ingest sports data", and click **Run workflow** once to seed your data. From then on it runs by itself on the schedule.

Notes:

- **A failed run turns red** and GitHub emails you (missing credentials, a database that's unreachable, an API outage), so you'll know if data stops flowing.
- **Updates:** `pip install fpl-context-mcp` grabs the latest release on every run, so fixes arrive automatically. Pin a version (`fpl-context-mcp==0.3.0`) if you'd rather upgrade on purpose.
- **Cost:** each run takes about a minute or two, so a twice-daily schedule stays well inside GitHub's free monthly minutes for private repos.
- **Why private:** GitHub automatically pauses scheduled workflows in *public* repos after 60 days without a commit. Private repos aren't paused.

### Option C — any other scheduler

Managed cron (Render, Railway, Fly.io machines, GCP Cloud Scheduler + Cloud Run Jobs, AWS EventBridge + Lambda/Fargate, systemd timers, Airflow, Dagster, etc.) all work the same way — point it at `fpl-context-ingest-press` and `fpl-context-ingest-match` (or the `python -m jobs.*` equivalents) with the cadence table above and the environment variables from [Configuration](#configuration).

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
        "DATABASE_URL": "postgresql://fpl_readonly:password@localhost:5432/fpl",
        "PINECONE_API_KEY": "pcsk_..."
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
        "DATABASE_URL": "postgresql://fpl_readonly:password@localhost:5432/fpl",
        "PINECONE_API_KEY": "pcsk_..."
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

Restart Claude Desktop. You should see `fpl-context` appear in the tools panel. If either tool returns nothing useful, re-check [Seeding data](#seeding-data-required-before-first-use) and [Keeping data fresh](#keeping-data-fresh-ongoing) before assuming the server itself is broken.

---

## Running the server standalone

```bash
# If installed from PyPI
fpl-context-mcp

# If running from source
python server.py
```

The server communicates over stdio — it is designed to be launched by an MCP client, not run as a persistent HTTP service. Running it directly is mainly useful for smoke-testing startup and environment variable loading.

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

✅ Pinecone          connected (index: 'fpl-context')
✅ PostgreSQL (RO)   connected (localhost:5432/fpl)
✅ PostgreSQL (ETL)  connected (localhost:5432/fpl)
✅ Guardian API      registered key configured

✅ All required components OK
```

The command exits with code `0` if all required components pass, or `1` if any required component fails. Optional components (Guardian API) emit warnings but do not cause a non-zero exit — a missing `GUARDIAN_API_KEY` just means Guardian articles are skipped. Note that `--check` only verifies *connectivity* — it doesn't tell you whether your tables/index actually have data in them; for that, see [Seeding data](#seeding-data-required-before-first-use).

---

## Dry-run mode

Set `DRY_RUN=true` to fetch data and verify routing without writing anything to Pinecone or PostgreSQL:

```bash
DRY_RUN=true fpl-context-mcp
DRY_RUN=true fpl-context-ingest-press
```

In dry-run mode:

- **Tools** return a human-readable description of the call that *would* have been made — the SQL with host, or the Pinecone index/namespace/params — without opening any connection.
- **Ingestion jobs** still call all external APIs (verifying connectivity) but skip every Pinecone and PostgreSQL write. Log output shows how many documents would have been upserted.
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

**Safety**

The tool enforces two layers of protection: a keyword blocklist rejects `INSERT`, `UPDATE`, `DELETE`, `DROP`, and similar statements before any database call is made, and the database connection uses a read-only role with no write grants.

---

### `query_press_conferences`

Semantic search over Premier League press coverage ingested from BBC Sport and The Guardian.

**Parameters**

| Parameter | Type | Default | Description |
|---|---|---|---|
| `query` | string | — | Natural-language question or topic |
| `top_k` | integer | 5 | Number of documents to return |
| `recency_weight` | float | 0.3 | Recency boost: `0.0` = pure semantic similarity, `1.0` = heavy recency bias |

**Ranking formula**

Results are re-ranked after retrieval:

```
final_score = semantic_score × (1 + recency_weight × recency_score)
```

`recency_score` is 1.0 for an article published today and decays toward 0.1 over 14 days.

**Example prompts**

- *"Any injury concerns for Saka this week?"*
- *"What did Slot say about Salah's contract situation?"*
- *"Who is doubtful for Arsenal's next match?"*

**No results?** If the `press` namespace hasn't been seeded yet, or everything in it has aged out past 14 days, this tool returns a message explaining that instead of an empty response — see [Keeping data fresh](#keeping-data-fresh-ongoing).

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
                 expected_goals, expected_assists, ict_index, status, news

fixtures         season_id, fpl_id, gw_number, kickoff_time,
                 home_team_fpl_id, away_team_fpl_id, home_score, away_score,
                 finished, home_team_difficulty, away_team_difficulty

gw_player_stats  season_id, player_fpl_id, gw_number, fixture_fpl_id,
                 opponent_team_fpl_id, was_home, minutes, goals_scored,
                 assists, clean_sheets, bonus, total_points,
                 expected_goals, expected_assists, ict_index, starts
```

**Current vs past seasons:** `teams`, `gameweeks`, `fixtures` and `gw_player_stats` hold the **current season only**. `players` also holds one totals-only row per player per past season (see [Seeding data](#seeding-data-required-before-first-use) for what that covers and what it misses).

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
| `tests/test_tools_press.py` | Pinecone query, recency re-ranking, degradation, dry-run |
| `tests/test_ingest_press_content.py` | BBC/Guardian fetchers, deduplication, orchestration, dry-run |
| `tests/test_ingest_match_data.py` | Fixture/gameweek upserts, stats selection, thread coordination, rollback, dry-run |
| `tests/test_backfill_history.py` | Past-season backfill: season handling, NULL team, best-effort ALTER, exit codes |

---

## Extending with new press sources

To add a new press source, subclass `_BaseFetcher` in `jobs/ingest_press_content.py` and add an instance to the `FETCHERS` list. The orchestrator picks it up automatically — no other changes needed.

```python
class MySportsFetcher(_BaseFetcher):
    source_name = "My Sports Site"

    def fetch(self) -> list[tuple[str, str, dict]]:
        # return a list of (doc_id, text, metadata) tuples
        ...

FETCHERS: list[_BaseFetcher] = [BBCSportFetcher(), GuardianAPIFetcher(), MySportsFetcher()]
```

Each tuple is `(doc_id, text, metadata)` where:

- `doc_id` — a stable 32-char hex ID (use `_doc_id(source + url)`)
- `text` — the full text to embed, prefixed with the source name
- `metadata` — must include `type`, `source`, `recency_score`, and `pub_timestamp`
