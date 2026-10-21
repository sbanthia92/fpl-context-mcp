"""Tests for the pure helpers in scripts/canary.py."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "canary", Path(__file__).resolve().parent.parent / "scripts" / "canary.py"
)
canary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canary)


def test_describe_unwraps_exception_groups():
    group = ExceptionGroup("boom", [ConnectionRefusedError("refused"), ValueError("bad")])
    assert canary.describe(group) == "ConnectionRefusedError: refused; ValueError: bad"
    assert canary.describe(KeyError("k")) == "KeyError: 'k'"


def test_looks_like_players():
    table = "player | team | pos\n-----\nSaka | ARS | MID"
    assert canary.looks_like_players(table)
    assert not canary.looks_like_players("No players match those filters.")
    assert not canary.looks_like_players("Error: could not reach the FPL API (boom).")
    assert not canary.looks_like_players("player | team\n-----")
