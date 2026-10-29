# fpl-context-mcp

<!-- mcp-name: io.github.sbanthia92/fpl-context-mcp -->

An [MCP](https://modelcontextprotocol.io) server that gives any MCP-capable AI agent live Fantasy Premier League (FPL) data: the current gameweek and deadline, fixtures with difficulty ratings, player stats and match history, any manager's squad and chips, expected points, and injury and suspension flags. **It works with zero setup and needs no database**: it reads the public FPL API directly. It runs locally in Claude Desktop, Claude Code, Cursor, VS Code Copilot, Windsurf, Gemini CLI and Codex; ChatGPT and other clients that only accept a URL can connect when you [host it over HTTP](#remote-access-over-http-chatgpt-and-other-url-only-clients).

| Tool | What it does |
|---|---|
| `get_current_gameweek` | The current and next gameweek and the transfer deadline |
| `get_fixtures` | Fixtures with FPL's difficulty rating (FDR, 1 to 5): every team, one team's run, or a single gameweek |
| `get_fixture_run` | How hard a team's next few fixtures are (average FDR, home/away, blank and double gameweeks), or a ranked fixture ticker for all 20 teams |
| `search_players` | Find and rank players by name, team, position and max price; sort by points, form, price, ownership, expected points and more |
| `get_player_history` | A player's matches this season (optionally against one opponent) and their season totals for past seasons |
| `get_gameweek_stats` | Every player's stats for one gameweek, ranked: top scorers, clean sheets, bonus |
| `get_xpts` | Estimated expected points for a gameweek by position, team and price, built from underlying stats, expected minutes, availability and fixture difficulty |
| `get_manager_squad` | A manager's squad, captain, bench, bank, transfers and chips used, from their FPL team ID |
| `get_live_availability` | Every player's current injury, suspension and availability flags |

Plus three prompts (`captain_pick`, `transfer_plan`, `chip_timing`) that show up as slash commands in clients that support them, and a reference resource (`fpl://glossary`). See [Prompts and resources](#prompts-and-resources).

> **Data limits.** FPL serves match-by-match data for the **current season only**; past seasons are season totals. Questions like "how did X do against Arsenal in earlier seasons" can't be answered from FPL's data, and the tools say so rather than guess.

---

## Contents

- [Quickstart](#quickstart)
- [Installation](#installation)
- [Configuration](#configuration)
- [Registering with Claude Desktop](#registering-with-claude-desktop)
- [Other AI clients (local)](#other-ai-clients-local)
- [Remote access over HTTP (ChatGPT and other URL-only clients)](#remote-access-over-http-chatgpt-and-other-url-only-clients)
- [Running the server standalone](#running-the-server-standalone)
- [Verifying connectivity (--check)](#verifying-connectivity---check)
- [MCP tools reference](#mcp-tools-reference)
- [Prompts and resources](#prompts-and-resources)
- [Architecture](#architecture)
- [Running tests](#running-tests)
- [Data sources and disclaimer](#data-sources-and-disclaimer)
- [License](#license)

---

## Quickstart

```bash
uvx fpl-context-mcp
```

Register it with your client ([Claude Desktop](#registering-with-claude-desktop) config: `{"mcpServers": {"fpl-context": {"command": "uvx", "args": ["fpl-context-mcp"]}}}`) and ask *"who should I captain this week?"* or *"show Arsenal's next five fixtures"*. That's it: no database, no account, no API key.

For other clients see [Other AI clients](#other-ai-clients-local). For ChatGPT and other URL-only clients, [host it over HTTP](#remote-access-over-http-chatgpt-and-other-url-only-clients).

---

## Installation

Needs Python 3.11+ and outbound HTTPS access to `fantasy.premierleague.com`.

### Without installing (uv)

`uvx fpl-context-mcp` runs it straight from PyPI. Requires [uv](https://docs.astral.sh/uv/).

### From PyPI

```bash
pip install fpl-context-mcp
```

This installs the `fpl-context-mcp` command.

### From source

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

The server needs **no configuration** over stdio. Everything below applies only to the HTTP transport (`--transport http`), and can be set as environment variables or in a `.env` file (see [`.env.example`](.env.example)):

| Variable | Default | Purpose |
|---|---|---|
| `MCP_TRANSPORT` / `MCP_HOST` / `MCP_PORT` (or `PORT`) | `stdio` / `127.0.0.1` / `8000` | Defaults for `--transport` / `--host` / `--port` |
| `MCP_AUTH_TOKEN` | (empty) | Require `Authorization: Bearer <token>` on `/mcp`. Comma-separate several keys |
| `MCP_RATE_LIMIT_PER_MIN` | `0` (off) | Per-caller request limit on `/mcp`; see [Remote access](#remote-access-over-http-chatgpt-and-other-url-only-clients) |
| `MCP_CLIENT_IP_HEADER` | (empty) | Header with the real client IP behind a trusted proxy |

---

## Registering with Claude Desktop

Add the server to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows).

### With uvx (recommended)

```json
{
  "mcpServers": {
    "fpl-context": {
      "command": "uvx",
      "args": ["fpl-context-mcp"]
    }
  }
}
```

### If installed with pip

```json
{
  "mcpServers": {
    "fpl-context": {
      "command": "fpl-context-mcp"
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
      "args": ["/absolute/path/to/fpl-context-mcp/server.py"]
    }
  }
}
```

> **Tip:** If you use `uv`, replace `"python"` with `"uv"` and prepend `"run"` to `args`:
> ```json
> "command": "uv",
> "args": ["run", "/absolute/path/to/fpl-context-mcp/server.py"]
> ```

Restart Claude Desktop. You should see `fpl-context` appear in the tools panel.

---

## Other AI clients (local)

Any client that can launch a local MCP server (stdio) works the same way: run the `fpl-context-mcp` command (no configuration needed).

**Claude Code**

```bash
claude mcp add fpl-context -- uvx fpl-context-mcp
```

**Cursor** (`~/.cursor/mcp.json`), **Windsurf** (`~/.codeium/windsurf/mcp_config.json`) and **Gemini CLI** (`~/.gemini/settings.json`) all use the same `mcpServers` shape as Claude Desktop:

```json
{
  "mcpServers": {
    "fpl-context": {
      "command": "uvx",
      "args": ["fpl-context-mcp"]
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
      "command": "uvx",
      "args": ["fpl-context-mcp"]
    }
  }
}
```

**OpenAI Codex CLI** — `~/.codex/config.toml`:

```toml
[mcp_servers.fpl-context]
command = "uvx"
args = ["fpl-context-mcp"]
```

**uv not installed?** `pip install fpl-context-mcp`, then use `"command": "fpl-context-mcp"` (no `args`) in any of the configs above.

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

**Several keys.** `MCP_AUTH_TOKEN` accepts a comma-separated list (`key-alice,key-bob`). Each user can have their own key, and removing one from the list revokes it without touching the others. Rate limits are applied per key.

**Rate limiting and load.** Set these before exposing a server publicly:

| Variable | Default | Purpose |
|---|---|---|
| `MCP_RATE_LIMIT_PER_MIN` | `0` (off) | Max requests per minute to `/mcp` for each caller (their bearer token, else their IP). Over the limit gets `429` with a `Retry-After` header. One tool call is typically 2-3 requests, so leave headroom (120 is a reasonable start). `/health` is never limited. The counter is in memory, so with several replicas each enforces its own limit. |
| `MCP_CLIENT_IP_HEADER` | (empty) | Behind a proxy, every request appears to come from the proxy. Set this to the header your proxy fills with the real client IP (`Fly-Client-IP`, `CF-Connecting-IP`, `X-Forwarded-For`). Only do this when the proxy overwrites the header; otherwise callers can fake their IP to dodge the limit. |

> **ChatGPT and claude.ai connectors only support OAuth or no authentication — not a static bearer token.** To use them you currently have to leave `MCP_AUTH_TOKEN` unset (the endpoint is then open to anyone who finds the URL) or put an OAuth-capable proxy in front. If you run it open, anyone who finds the URL can use it and spend your server's FPL API traffic, so set the rate limit below. The server logs a warning at startup when it's bound to a non-local address without a token or a rate limit.

**Running a public endpoint?** See the [hosting guide](docs/hosting.md): container, proxy setup, auth modes, and a canary that checks the endpoint every 15 minutes.

**Container.** A [`Dockerfile`](Dockerfile) is included: `docker build -t fpl-context-mcp .`, then run it (optionally with `MCP_AUTH_TOKEN` set). It runs as a non-root user, listens on `$MCP_PORT` / `$PORT` / 8000, defaults to a rate limit of 120 requests a minute, and has a health check on `/health`.

**Where the data comes from.** A hosted server calls the public FPL API on behalf of whoever connects, and caches responses in memory. There is nothing to provision or keep fresh. Because you're now serving results to other people, see [Data sources and disclaimer](#data-sources-and-disclaimer), and the [hosting guide](docs/hosting.md).

---

## Running the server standalone

```bash
fpl-context-mcp          # installed from PyPI
python server.py         # from source
```

By default the server communicates over stdio: it is designed to be launched by an MCP client, and running it directly is mainly useful for smoke-testing. To run it as a persistent network service, use `--transport http` (see [Remote access over HTTP](#remote-access-over-http-chatgpt-and-other-url-only-clients)).

---

## Verifying connectivity (--check)

```bash
fpl-context-mcp --check
```

Confirms the FPL API is reachable from your machine and exits `0` on success, `1` on failure:

```
=== fpl-context-mcp check ===

✅ FPL API reachable (700 players, 20 teams)

✅ OK
```

---

## MCP tools reference

### Live tools overview

These tools call the public FPL API directly and need no database or configuration. They share one cache: FPL's `bootstrap-static` data (every player and team) is downloaded once and reused for 5 minutes across tools; a manager's picks and a player's match history are cached for 2 and 5 minutes, and a finished gameweek's stats for an hour. If FPL can't be reached, the last cached copy is served and labelled as possibly out of date. All are read-only and advertise `readOnlyHint`, so clients can skip approval prompts. Team arguments accept a full name, a short name or a unique fragment (`Arsenal`, `MCI`, `Man City`).

### `get_current_gameweek`

No parameters. Returns the current gameweek (in progress or finished), the next gameweek, and the next transfer deadline with the time left. *"When is the next deadline?"*

### `get_fixtures`

Fixtures with FPL's difficulty rating (FDR): 1 easiest, 5 hardest, from each team's own point of view.

| Parameter | Type | Description |
|---|---|---|
| `team` | string | Optional. Show that team's fixtures (opponent, home/away, its own FDR). Omit for all teams. |
| `gameweek` | integer 1-38 | Optional. A single gameweek (includes scores once played). |
| `gameweeks_ahead` | integer 1-38 | Optional. Gameweeks to list from the next one to play (default 5). Ignored when `gameweek` is set. |

*"Arsenal's next six fixtures?"*, *"What are the fixtures in gameweek 12?"*

### `get_fixture_run`

| Parameter | Type | Description |
|---|---|---|
| `team` | string | Optional. Omit to rank all 20 teams, easiest run first. |
| `gameweeks` | integer 1-38 | Optional. Window length starting from the next gameweek to play (default 5). |

For one team you get the average FDR, home/away split, the fixture-by-fixture sequence and any blank or double gameweeks. *"Who has the best fixtures for the next six gameweeks?"*, *"Is Chelsea's run good enough to keep Palmer?"*

### `search_players`

| Parameter | Type | Description |
|---|---|---|
| `name` | string | Part of a first, second or web name. |
| `team` | string | Team name or short name. |
| `position` | `GKP` / `DEF` / `MID` / `FWD` | Optional. |
| `max_price` | number | Maximum price in £m, e.g. `7.5`. |
| `sort_by` | string | `total_points` (default), `form`, `price`, `selected_by_percent`, `ep_next`, `points_per_game`, `goals_scored`, `assists`, `clean_sheets`, `minutes`, `transfers_in_event`. Highest first. |
| `limit` | integer | Rows to return (default 15, max 50). |

Each row has price, total points, form, ownership, FPL's expected points for the next gameweek (`ep_next`) and availability status. *"Best midfielders under £8m by form?"*

### `get_xpts`

| Parameter | Type | Description |
|---|---|---|
| `position` | `GKP` / `DEF` / `MID` / `FWD` | Optional. |
| `team` | string | Optional. |
| `max_price` | number | Optional, in £m. |
| `gameweek` | integer 1-38 | Optional. Defaults to the next gameweek to play. |
| `limit` | integer | Rows to return (default 15, max 50). |

Ranks players by estimated expected points (`xPts`) next to the opponent and its FDR, expected minutes, and FPL's own `ep_next`. Double gameweeks count both fixtures; blank ones score zero; flagged players are discounted by FPL's chance of playing.

**How `xPts` is computed.** A simple, transparent baseline, not a forecast model: appearance points, plus the player's season xG, xA, clean-sheet, saves and bonus rates per 90 times FPL's scoring values, scaled by expected minutes (the share of available minutes played so far), multiplied by ±10% per FDR point from a neutral 3, and discounted by availability. It extrapolates the season so far, so it is noisy in the first few gameweeks and cannot see rotation, tactics or set-piece changes. Use it to build a shortlist, then cross-check with `get_live_availability`. *"Top five midfielders under £8m by expected points?"*

### `get_player_history`

| Parameter | Type | Description |
|---|---|---|
| `player` | string | Required. First, second or full name (`Saka`, `Bukayo Saka`). If several players match, the error lists them so you can be specific. |
| `opponent` | string | Optional. Only this season's matches against that team. |
| `last_n` | integer 1-40 | Optional. Only the most recent N matches. |

Returns this season's match-by-match record (opponent, home/away, score, minutes, goals, assists, clean sheets, bonus, xG, xA, points), totals for the matches shown, and season totals for every past season FPL has on record. *"How has Haaland done against Arsenal this season?"*, *"Show Saka's last five matches."*

**What FPL does and doesn't have.** FPL serves match-by-match data for the **current season only**. Past seasons are one row of totals each. So "how did X perform against Arsenal over the last few seasons" cannot be answered, and the tool says so instead of implying the player never faced them.

### `get_gameweek_stats`

| Parameter | Type | Description |
|---|---|---|
| `gameweek` | integer 1-38 | Optional. Defaults to the latest gameweek with results. |
| `position` | `GKP` / `DEF` / `MID` / `FWD` | Optional. |
| `team` | string | Optional. |
| `sort_by` | string | `total_points` (default), `goals_scored`, `assists`, `bonus`, `minutes`, `expected_goals`, `expected_assists`. Highest first. |
| `limit` | integer | Rows to return (default 15, max 50). |

Every player with minutes in that gameweek, ranked. A gameweek still in progress is labelled, since points and bonus can still change. *"Top scorers last gameweek?"*, *"Which defenders kept a clean sheet in GW9?"*

### `get_manager_squad`

| Parameter | Type | Description |
|---|---|---|
| `team_id` | integer | The manager's FPL team ID: the number in the URL of their FPL Points page (`fantasy.premierleague.com/entry/<id>/event/<gw>`). |
| `gameweek` | integer 1-38 | Optional. Defaults to the manager's current gameweek. |

Returns the 15-man squad with captain and vice-captain, bench, bank, squad value, transfers and any hit taken, the chip played that week, and every chip used so far this season. It reads only the public data FPL already shows anyone with the team ID; no login. *"What chips has team 123456 used?"*

### `get_live_availability`

Current FPL injury, suspension and availability flags, fetched live from the FPL API. Use it before recommending or captaining a player, so a newly flagged player isn't missed.

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
- If FPL can't be reached, the last cached copy is served and labelled as possibly out of date. With no cached copy it returns an error.

---

## Prompts and resources

### Prompts

Ready-made workflows. They contain instructions only, no data, so they never go stale: the tools fetch live FPL data when the prompt runs. Clients that support MCP prompts show them as slash commands (for example `/mcp__fpl-context__captain_pick` in Claude Code). Every argument is optional; without `team_id` the model asks for your squad or answers generally.

| Prompt | Arguments | What it does |
|---|---|---|
| `captain_pick` | `team_id`, `gameweek` | Ranks candidates with `get_xpts`, checks fixtures, opponent history and availability, and recommends a captain, vice-captain and a differential |
| `transfer_plan` | `team_id`, `free_transfers`, `gameweeks` | Finds your weakest assets, compares fixture runs and expected points for replacements within budget, and recommends one or two transfers (and whether a hit is worth it) |
| `chip_timing` | `team_id`, `chip` (`wildcard`, `freehit`, `bboost`, `3xc`) | Looks for double and blank gameweeks and suggests when to play each chip you have left |

### Resources

| URI | Contents |
|---|---|
| `fpl://glossary` | FPL status codes, FDR, positions, the data limits, and which tool answers what |

---

## Architecture

How the server is built (layers, request flow, caching, transports) and why, including the design decisions and their trade-offs, is in [docs/architecture.md](docs/architecture.md).

---

## Running tests

```bash
pip install -e ".[dev]"
pytest tests/ -v          # all mocked: no network access needed
ruff check . && ruff format .
```

---

## Data sources and disclaimer

fpl-context-mcp is an independent open-source project. It is **not affiliated with, endorsed by, or sponsored by** the Premier League or Fantasy Premier League.

The package ships no data. It fetches it at run time, from your machine, from:

| Source | Used for | Notes |
|---|---|---|
| Fantasy Premier League API (`fantasy.premierleague.com/api`) | Players, teams, fixtures, match stats, manager squads, injury/availability news | Unofficial and undocumented; it can change or rate-limit without notice. Responses are cached in memory for a few minutes to keep traffic low. |

**You are responsible for complying with the source's terms of use** for the data you retrieve and, if you [host the server](#remote-access-over-http-chatgpt-and-other-url-only-clients) for other people, serve. This is especially relevant for commercial use and for public deployments. The MIT license below covers this project's code only, not any third-party content it retrieves.

---

## License

[MIT](LICENSE) © 2026 Shubham Banthia
