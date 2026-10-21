"""
MCP prompts: ready-made FPL workflows that clients surface as slash commands.

Each prompt is a short instruction that tells the model which tools to call, in what
order, and how to present the answer. They contain no data themselves, so they never
go stale: the tools fetch live FPL data when the prompt runs.
"""

import mcp.types as types

_CHECK_AVAILABILITY = (
    "Check every candidate with get_live_availability before recommending them, and "
    "drop or flag anyone who is injured, suspended, doubtful or has a recent news note."
)

_SQUAD = (
    "My FPL team ID is {team_id}. Call get_manager_squad with it to see my squad, bank, "
    "free-transfer picture and chips used."
)
_NO_SQUAD = (
    "I haven't given you my team ID, so ask me for it (or for my squad) if you need it; "
    "otherwise answer generally."
)


def _squad_line(team_id: str | None) -> str:
    return _SQUAD.format(team_id=team_id) if team_id else _NO_SQUAD


def _captain_pick(args: dict[str, str]) -> str:
    gw = args.get("gameweek")
    target = f"gameweek {gw}" if gw else "the next gameweek"
    return (
        f"Help me choose my captain for {target}.\n\n"
        f"1. {_squad_line(args.get('team_id'))}\n"
        "2. Call get_current_gameweek so you know the deadline and which gameweek this is.\n"
        "3. Call get_xpts (limit 15) to rank candidates, and prefer players from my squad.\n"
        "4. Call get_fixture_run for the top candidates' teams to check the fixture, "
        "home/away and any blank or double gameweek, and get_player_history (with the "
        "opponent) for how each candidate has done against that side this season.\n"
        f"5. {_CHECK_AVAILABILITY}\n\n"
        "Answer with a captain, a vice-captain, and one differential option, each with "
        "one or two sentences of reasoning (expected points, fixture, minutes security). "
        "Mention the deadline. Be clear that expected points are estimates."
    )


def _transfer_plan(args: dict[str, str]) -> str:
    free = args.get("free_transfers")
    free_line = (
        f"I have {free} free transfer(s)."
        if free
        else "Ask me how many free transfers I have if it affects the plan."
    )
    horizon = args.get("gameweeks") or "5"
    return (
        f"Help me plan my FPL transfers over the next {horizon} gameweeks.\n\n"
        f"1. {_squad_line(args.get('team_id'))} {free_line}\n"
        f"2. Call get_fixture_run with gameweeks={horizon} (no team) to see which teams have "
        "the best and worst runs, and flag blanks and doubles.\n"
        "3. Find my weakest assets: players who are flagged, out of form, on bad runs or "
        "not starting. Use get_live_availability for my squad's names.\n"
        "4. For each position I want to improve, call get_xpts and search_players with "
        "the position and a max_price equal to the player's sale price plus my bank.\n"
        f"5. {_CHECK_AVAILABILITY}\n\n"
        "Recommend one or two concrete transfers (player out, player in, price, why), say "
        "whether a hit is worth it and what it costs, and name a fallback if the first "
        "choice is flagged before the deadline. Keep the squad legal: 15 players, "
        "at most 3 from one club, within budget."
    )


def _chip_timing(args: dict[str, str]) -> str:
    chip = args.get("chip")
    chip_line = (
        f"I'm asking about my {chip} chip."
        if chip
        else "Cover each chip I still have: Wildcard, Free Hit, Bench Boost, Triple Captain."
    )
    return (
        "Help me decide when to play my FPL chips.\n\n"
        f"1. {_squad_line(args.get('team_id'))} Work out which chips I have already used "
        "this season. FPL splits some chips into halves of the season, so say what you are "
        "assuming and ask me if it matters. " + chip_line + "\n"
        "2. Call get_current_gameweek for where we are in the season.\n"
        "3. Call get_fixture_run with a long window (gameweeks=10) and no team to find "
        "double gameweeks (good for Bench Boost and Triple Captain) and blank gameweeks "
        "(good for Free Hit). Cross-check individual teams with get_fixtures.\n"
        "4. For Bench Boost, check my bench strength with get_xpts; for Triple Captain, "
        "the best premium player's fixtures; for Wildcard, how much my squad would "
        "improve.\n\n"
        "Give a recommended gameweek for each chip I have left, the reason, and what would "
        "change your mind. Say plainly that future fixtures can be rescheduled."
    )


# name -> (Prompt descriptor, builder(args) -> text)
_PROMPTS: dict[str, tuple[types.Prompt, object]] = {}


def _register(name: str, title: str, description: str, args: list[tuple[str, str, bool]], build):
    _PROMPTS[name] = (
        types.Prompt(
            name=name,
            title=title,
            description=description,
            arguments=[types.PromptArgument(name=n, description=d, required=r) for n, d, r in args],
        ),
        build,
    )


_TEAM_ID = (
    "team_id",
    "Your FPL team ID (the number in the URL of your FPL Points page). Optional.",
    False,
)

_register(
    "captain_pick",
    "Captain pick",
    "Recommend a captain, vice-captain and differential for a gameweek.",
    [_TEAM_ID, ("gameweek", "Gameweek to pick for (default: the next one).", False)],
    _captain_pick,
)
_register(
    "transfer_plan",
    "Transfer plan",
    "Plan one or two transfers using fixture runs, expected points and availability.",
    [
        _TEAM_ID,
        ("free_transfers", "How many free transfers you have.", False),
        ("gameweeks", "How many gameweeks ahead to plan for (default 5).", False),
    ],
    _transfer_plan,
)
_register(
    "chip_timing",
    "Chip timing",
    "Decide when to play Wildcard, Free Hit, Bench Boost and Triple Captain.",
    [
        _TEAM_ID,
        (
            "chip",
            "Only this chip: wildcard, freehit, bboost or 3xc (default: all you have left).",
            False,
        ),
    ],
    _chip_timing,
)


def list_prompts() -> list[types.Prompt]:
    return [prompt for prompt, _ in _PROMPTS.values()]


def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    """
    Render a prompt.

    Raises:
        ValueError: for an unknown prompt name or a non-numeric team_id / gameweek /
                    free_transfers / gameweeks value.
    """
    entry = _PROMPTS.get(name)
    if entry is None:
        raise ValueError(f"Unknown prompt: {name!r}")
    args = {k: str(v).strip() for k, v in (arguments or {}).items() if v not in (None, "")}
    for key in ("team_id", "gameweek", "free_transfers", "gameweeks"):
        if key in args and not args[key].isdigit():
            raise ValueError(f"{key} must be a whole number, got {args[key]!r}")
    if "chip" in args and args["chip"] not in ("wildcard", "freehit", "bboost", "3xc"):
        raise ValueError("chip must be one of: wildcard, freehit, bboost, 3xc")
    prompt, build = entry
    return types.GetPromptResult(
        description=prompt.description,
        messages=[
            types.PromptMessage(
                role="user", content=types.TextContent(type="text", text=build(args))
            )
        ],
    )
