# Hosting fpl-context-mcp as a public endpoint

This guide is for running **one shared server** that many people connect to by URL, such
as the maintainer's hosted endpoint, with no install on their side. If you only want it
for yourself, the [README](../README.md) is enough.

A hosted server is the normal MCP server run with `--transport http`, in a container. It
needs **no database and no ingestion**: it calls the public FPL API on behalf of callers and
caches responses in memory. Nothing about it is specific to one cloud.

## Checklist

1. **Build and run the image.**

   ```bash
   docker build -t fpl-context-mcp .
   docker run -p 8000:8000 \
     -e MCP_AUTH_TOKEN=key-alice,key-bob \
     -e MCP_RATE_LIMIT_PER_MIN=120 \
     fpl-context-mcp
   ```

   The image listens on `$MCP_PORT`, else `$PORT` (set by Render, Cloud Run, Fly and
   similar), else 8000, runs as a non-root user, and has a health check on `/health`.
2. **Terminate TLS** in front of it. Every managed platform does; the server speaks plain
   HTTP. The MCP endpoint is `https://<your-host>/mcp`.
3. **Tell the server about the proxy.** Behind a proxy every request looks like it comes
   from the proxy, which would put all callers in one rate-limit bucket. Set
   `MCP_CLIENT_IP_HEADER` to the header your platform fills with the real client IP, for
   example `Fly-Client-IP` (Fly.io), `CF-Connecting-IP` (Cloudflare) or `X-Forwarded-For`.
   Only set it when the proxy overwrites that header.
4. **Monitor it.** [`.github/workflows/canary.yml`](../.github/workflows/canary.yml) calls
   the live endpoint every 15 minutes and fails when it is down or erroring. See
   [Canary](#canary).

## Choosing an auth mode

| Mode | When | Notes |
|---|---|---|
| Open (no `MCP_AUTH_TOKEN`) | ChatGPT and claude.ai connectors | These clients support only OAuth or no auth, not a static bearer token. The rate limit is what protects you. |
| Bearer keys (`MCP_AUTH_TOKEN=a,b,c`) | Claude Code, Cursor, the Agents SDK, anything that can set a header | Per-user keys, each rate-limited separately, individually revocable by removing it from the list and redeploying. |

You can run both by deploying two instances (one open and tightly limited, one keyed with
higher limits).

## Sizing

There is no database to size. The server is lightweight; one small instance handles
substantial traffic because all tools share one in-memory cache (the FPL `bootstrap-static`
download is reused for 5 minutes). The rate limit is per process: with N replicas, multiply
accordingly. A cold start makes one FPL request; if FPL is unreachable the server serves
stale cached data where it has some.

## Canary

`scripts/canary.py` connects to the endpoint over streamable HTTP and checks that it:

1. initialises and lists tools,
2. answers a live tool (`get_current_gameweek`), and
3. returns real player data from FPL (`search_players`).

The workflow runs every 15 minutes and on demand. Configure it with a repository variable
`MCP_CANARY_URL` (for example `https://mcp.example.com/mcp`) and, if the endpoint uses
keys, a secret `MCP_CANARY_TOKEN`. A failing run emails the repository owner by default. It
also keeps a platform that sleeps idle instances awake.

## What you take on

- **Uptime and cost.** Small, but yours.
- **Abuse.** The rate limit bounds it; watch the logs for 429s.
- **FPL terms and traffic.** A hosted server sends all callers' requests to FPL from one IP.
  FPL's terms are informal and the API is unofficial. The shared cache keeps traffic low
  (a handful of requests a minute regardless of callers for the common tools), but
  per-player and per-gameweek tools add requests as callers vary. Keep the project's
  licensing guardrail in mind: only serve data from sources whose terms permit use through
  an AI tool.
