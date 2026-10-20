"""
MCP resources: static reference material a client can attach to the conversation.

  fpl://schema    — the database schema (tables, columns, notes) for writing correct SQL
                    with query_historical_stats; the same text as the tool description
  fpl://glossary  — FPL status codes, FDR, positions and which tool answers what
"""

import mcp.types as types
from pydantic import AnyUrl

from tools.query_historical_stats import SCHEMA_DESCRIPTION

SCHEMA_URI = "fpl://schema"
GLOSSARY_URI = "fpl://glossary"

GLOSSARY = """\
FPL glossary

Positions: GKP goalkeeper, DEF defender, MID midfielder, FWD forward.
Prices are in £m (the API stores tenths: 100 = £10.0m).

Availability status codes: a available, d doubtful, i injured, s suspended,
u unavailable (left the club / on loan), n not in squad.
chance_of_playing is FPL's estimate for the next gameweek; empty means no concern.

FDR (fixture difficulty rating): 1 easiest to 5 hardest, from the team's own point of
view and set by FPL. Blank gameweek: a team has no fixture. Double gameweek: two.

ep_next: FPL's own expected points for the next gameweek. xPts (get_xpts): this server's
baseline estimate, built from per-90 xG/xA/clean-sheet/save/bonus rates, expected minutes,
availability and FDR. Both are estimates.

Which tool answers what:
  get_current_gameweek   deadline, what gameweek it is
  get_fixtures           the fixture list with FDR
  get_fixture_run        how hard a team's next N fixtures are; a ranked fixture ticker
  search_players         find and rank players by stat, team, position, price
  get_xpts               expected-points shortlist for a gameweek
  get_manager_squad      a manager's squad and chips from their team ID
  get_live_availability  current injuries and suspensions
  query_historical_stats SQL over your own history database (only when configured)
"""

_RESOURCES: dict[str, tuple[types.Resource, str]] = {
    SCHEMA_URI: (
        types.Resource(
            uri=AnyUrl(SCHEMA_URI),
            name="schema",
            title="FPL database schema",
            description="Tables and columns available to query_historical_stats.",
            mimeType="text/plain",
        ),
        SCHEMA_DESCRIPTION.strip(),
    ),
    GLOSSARY_URI: (
        types.Resource(
            uri=AnyUrl(GLOSSARY_URI),
            name="glossary",
            title="FPL glossary and tool guide",
            description="Status codes, FDR, positions and which tool answers what.",
            mimeType="text/plain",
        ),
        GLOSSARY,
    ),
}


def list_resources() -> list[types.Resource]:
    return [resource for resource, _ in _RESOURCES.values()]


def read_resource(uri: str) -> str:
    """Return the text of a resource. Raises ValueError for an unknown URI."""
    entry = _RESOURCES.get(str(uri))
    if entry is None:
        raise ValueError(f"Unknown resource: {str(uri)!r}")
    return entry[1]
