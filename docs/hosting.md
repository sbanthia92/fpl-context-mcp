# Hosting fpl-context-mcp as a public endpoint

This guide is for running **one shared server** that many people connect to by URL, such
as the maintainer's hosted endpoint, with no install on their side. If you only want it
for yourself, the [README](../README.md) is enough.

A hosted server is the normal MCP server run with `--transport http`, in a container, in
front of a PostgreSQL database you keep fresh. Nothing about it is specific to one cloud.

## Checklist

1. **Database.** Provision Postgres and run [`db/schema.sql`](../db/schema.sql). Seed and
   schedule ingestion as in the README (the `ingest_match_data.yml` workflow in this repo
   is a working example).
2. **Lock the database role down.** Run [`db/hosted_role.sql`](../db/hosted_role.sql) to
   create a `fpl_readonly` role with SELECT on the six tables only, a connection limit,
   read-only transactions and server-side timeouts. On a public endpoint this role, not
   the server's keyword blocklist, is what limits the damage from arbitrary `SELECT`s.
3. **Build and run the image.**

   ```bash
   docker build -t fpl-context-mcp .
   docker run -p 8000:8000 \
     -e DATABASE_URL=postgresql://fpl_readonly:...@host:5432/fpl \
     -e MCP_AUTH_TOKEN=key-alice,key-bob \
     -e MCP_RATE_LIMIT_PER_MIN=120 \
     fpl-context-mcp
   ```

   The image listens on `$MCP_PORT`, else `$PORT` (set by Render, Cloud Run, Fly and
   similar), else 8000, runs as a non-root user, and has a health check on `/health`.
4. **Terminate TLS** in front of it. Every managed platform does; the server speaks plain
   HTTP. The MCP endpoint is `https://<your-host>/mcp`.
5. **Tell the server about the proxy.** Behind a proxy every request looks like it comes
   from the proxy, which would put all callers in one rate-limit bucket. Set
   `MCP_CLIENT_IP_HEADER` to the header your platform fills with the real client IP, for
   example `Fly-Client-IP` (Fly.io), `CF-Connecting-IP` (Cloudflare) or `X-Forwarded-For`.
   Only set it when the proxy overwrites that header.
6. **Monitor it.** [`.github/workflows/canary.yml`](../.github/workflows/canary.yml) calls
   the live endpoint every 15 minutes and fails when it is down, errors, or its data has
   fallen behind FPL (a stalled ingestion). See [Canary](#canary).

## Choosing an auth mode

| Mode | When | Notes |
|---|---|---|
| Open (no `MCP_AUTH_TOKEN`) | ChatGPT and claude.ai connectors | These clients support only OAuth or no auth, not a static bearer token. Rate limiting and the hardened role are what protect you. |
| Bearer keys (`MCP_AUTH_TOKEN=a,b,c`) | Claude Code, Cursor, the Agents SDK, anything that can set a header | Per-user keys, each rate-limited separately, individually revocable by removing it from the list and redeploying. |

You can run both by deploying two instances against the same database (one open and
tightly limited, one keyed with higher limits).

## Sizing

The data is a few MB per season, so a free-tier Postgres is plenty. The server is
lightweight; one small instance handles substantial traffic because the live tools share a
cache and SQL is capped at `MCP_MAX_CONCURRENT_QUERIES` per process. Rate limits and the
query cap are per process: with N replicas, multiply accordingly.

## Canary

`scripts/canary.py` connects to the endpoint over streamable HTTP and checks that it:

1. initialises and lists tools,
2. answers a live tool (`get_current_gameweek`), and
3. (if the SQL tool is offered) returns data that is not behind FPL by more than one
   finished gameweek, which catches a stalled ingestion job.

The workflow runs every 15 minutes and on demand. Configure it with a repository
variable `MCP_CANARY_URL` (for example `https://mcp.example.com/mcp`) and, if the endpoint
uses keys, a secret `MCP_CANARY_TOKEN`. A failing run emails the repository owner by
default. It also keeps a platform that sleeps idle instances awake.

## What you take on

- **Uptime and cost.** Small, but yours.
- **Abuse.** Rate limits and the role bound it; watch the logs for 429s and slow queries.
- **FPL terms.** The server and its database serve FPL data to the public. FPL's terms are
  informal and this concentrates traffic on one IP, so keep ingestion polite (the repo's
  jobs are) and keep the project's licensing guardrail in mind: only serve data from
  sources whose terms permit use through an AI tool.
