"""
MCP tool: get_current_gameweek

Which FPL gameweek is live or next, and when its transfer deadline falls. Reads FPL's
bootstrap-static (shared, cached). Needs no database.
"""

import logging
from datetime import UTC, datetime

from tools import fpl_client

log = logging.getLogger(__name__)


def _describe(ev: dict, now: datetime) -> list[str]:
    lines = [f"Gameweek {ev['id']} ({ev['name']})"]
    deadline = ev.get("deadline_time")
    if deadline:
        when = datetime.fromisoformat(deadline.replace("Z", "+00:00"))
        delta = when - now
        if delta.total_seconds() > 0:
            hours = int(delta.total_seconds() // 3600)
            lines.append(f"  deadline: {deadline} (in {hours // 24}d {hours % 24}h)")
        else:
            lines.append(f"  deadline: {deadline} (passed)")
    return lines


async def get_current_gameweek() -> str:
    """
    Current and next gameweek with transfer deadlines.

    Returns:
        A short plain-text summary, or an error message when FPL is unreachable.
    """
    try:
        boot, stale = await fpl_client.bootstrap()
    except Exception as exc:
        log.error("Could not fetch bootstrap-static: %s", exc, exc_info=True)
        return fpl_client.unreachable_error(exc)

    now = datetime.now(UTC)
    events = boot.get("events", [])
    current = next((e for e in events if e.get("is_current")), None)
    upcoming = next((e for e in events if e.get("is_next")), None)

    lines = []
    if current:
        state = "finished" if current.get("finished") else "in progress"
        lines.append(f"Current: gameweek {current['id']} — {state}")
        lines.append(f"  deadline was {current.get('deadline_time')}")
        if current.get("average_entry_score"):
            lines.append(f"  average score {current['average_entry_score']}")
    if upcoming:
        lines.append("Next:")
        lines.extend(_describe(upcoming, now))
    if not current and not upcoming:
        lines.append("No current or upcoming gameweek: the season is over or not yet published.")
    lines.append(f"Gameweeks finished: {sum(1 for e in events if e.get('finished'))} of 38")
    if stale:
        lines += ["", fpl_client.STALE_NOTE]
    return "\n".join(lines)
