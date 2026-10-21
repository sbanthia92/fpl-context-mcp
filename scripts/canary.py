"""
Canary for a hosted fpl-context-mcp endpoint.

Connects over streamable HTTP and checks that the server:
  1. initialises and lists tools,
  2. answers a live tool (get_current_gameweek), and
  3. returns real player data from the FPL API (search_players).

Usage:
    python scripts/canary.py https://mcp.example.com/mcp [--token KEY]
    (or set MCP_CANARY_URL / MCP_CANARY_TOKEN)

Exit status 0 when every check passes, 1 otherwise; failures are printed one per line.
"""

import argparse
import asyncio
import os
import sys

TIMEOUT_SECONDS = 30


def describe(exc: BaseException) -> str:
    """Flatten the ExceptionGroups that anyio raises into a readable message."""
    subs = getattr(exc, "exceptions", None)
    if subs:
        return "; ".join(describe(e) for e in subs)
    return f"{type(exc).__name__}: {exc}"


def looks_like_players(text: str) -> bool:
    """True when a search_players answer is a table with at least one data row."""
    lines = text.strip().splitlines()
    return len(lines) >= 3 and lines[0].startswith("player |")


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

                res = await session.call_tool("search_players", {"limit": 3})
                text = res.content[0].text if res.content else ""
                if res.isError or not looks_like_players(text):
                    failures.append(f"search_players returned no player data: {text[:200]}")
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
