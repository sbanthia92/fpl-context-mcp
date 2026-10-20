"""
Canary for a hosted fpl-context-mcp endpoint.

Connects over streamable HTTP and checks that the server:
  1. initialises and lists tools,
  2. answers a live tool (get_current_gameweek), and
  3. if the SQL tool is offered, holds data no more than one finished gameweek behind FPL
     (a stalled ingestion job otherwise goes unnoticed).

Usage:
    python scripts/canary.py https://mcp.example.com/mcp [--token KEY]
    (or set MCP_CANARY_URL / MCP_CANARY_TOKEN)

Exit status 0 when every check passes, 1 otherwise; failures are printed one per line.
"""

import argparse
import asyncio
import os
import sys

import requests

FPL_BOOTSTRAP = "https://fantasy.premierleague.com/api/bootstrap-static/"
MAX_GAMEWEEKS_BEHIND = 1
TIMEOUT_SECONDS = 30


def first_value(table: str) -> str | None:
    """First data cell of a plain-text table as returned by the SQL tool."""
    lines = table.strip().splitlines()
    if len(lines) < 3:
        return None
    return lines[2].split(" | ")[0].strip()


def data_is_stale(db_gw: int | None, live_finished_gw: int) -> bool:
    """True when the database is more than MAX_GAMEWEEKS_BEHIND gameweeks behind FPL."""
    if live_finished_gw == 0:  # season not started: nothing to be behind on
        return False
    return (db_gw or 0) < live_finished_gw - MAX_GAMEWEEKS_BEHIND


def live_finished_gameweek() -> int:
    resp = requests.get(
        FPL_BOOTSTRAP, timeout=TIMEOUT_SECONDS, headers={"User-Agent": "fpl-context-mcp-canary"}
    )
    resp.raise_for_status()
    return max((e["id"] for e in resp.json()["events"] if e.get("finished")), default=0)


def describe(exc: BaseException) -> str:
    """Flatten the ExceptionGroups that anyio raises into a readable message."""
    subs = getattr(exc, "exceptions", None)
    if subs:
        return "; ".join(describe(e) for e in subs)
    return f"{type(exc).__name__}: {exc}"


async def run(url: str, token: str = "") -> list[str]:
    """Run every check and return a list of failure messages (empty = healthy)."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    failures: list[str] = []
    headers = {"Authorization": f"Bearer {token}"} if token else None
    try:
        async with streamablehttp_client(url, headers=headers, timeout=TIMEOUT_SECONDS) as (
            read,
            write,
            _,
        ):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), TIMEOUT_SECONDS)
                tools = {t.name for t in (await session.list_tools()).tools}
                if "get_current_gameweek" not in tools:
                    failures.append(f"tool list is missing get_current_gameweek: {sorted(tools)}")
                else:
                    res = await session.call_tool("get_current_gameweek", {})
                    text = res.content[0].text if res.content else ""
                    if res.isError or text.startswith("Error"):
                        failures.append(f"get_current_gameweek failed: {text[:200]}")

                if "query_historical_stats" in tools:
                    res = await session.call_tool(
                        "query_historical_stats",
                        {"sql": "SELECT max(gw_number) AS gw FROM gw_player_stats"},
                    )
                    text = res.content[0].text if res.content else ""
                    value = first_value(text)
                    if res.isError or value is None:
                        failures.append(f"SQL tool failed: {text[:200]}")
                    else:
                        db_gw = int(value) if value.isdigit() else None
                        live_gw = live_finished_gameweek()
                        if data_is_stale(db_gw, live_gw):
                            failures.append(
                                f"database is stale: latest gameweek with stats is {db_gw}, "
                                f"FPL has finished {live_gw}. Is ingestion running?"
                            )
    except Exception as exc:  # connection refused, 401/429, timeouts, protocol errors
        failures.append(f"could not talk to {url}: {describe(exc)}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description="Health check for a hosted fpl-context-mcp.")
    parser.add_argument("url", nargs="?", default=os.getenv("MCP_CANARY_URL", ""))
    parser.add_argument("--token", default=os.getenv("MCP_CANARY_TOKEN", ""))
    args = parser.parse_args()
    if not args.url:
        print("No URL: pass one or set MCP_CANARY_URL.", file=sys.stderr)
        return 2
    failures = asyncio.run(run(args.url, args.token))
    for f in failures:
        print(f"FAIL: {f}")
    if not failures:
        print(f"OK: {args.url}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
