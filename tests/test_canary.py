"""Tests for the pure helpers in scripts/canary.py."""

import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location(
    "canary", Path(__file__).resolve().parent.parent / "scripts" / "canary.py"
)
canary = importlib.util.module_from_spec(spec)
spec.loader.exec_module(canary)


def test_first_value_reads_first_data_cell():
    assert canary.first_value("gw\n--\n7") == "7"
    assert canary.first_value("a | b\n-----\n3 | x") == "3"
    assert canary.first_value("Query returned no results.") is None


def test_first_value_handles_null():
    assert canary.first_value("gw\n--\nNone") == "None"


def test_describe_unwraps_exception_groups():
    group = ExceptionGroup("boom", [ConnectionRefusedError("refused"), ValueError("bad")])
    assert canary.describe(group) == "ConnectionRefusedError: refused; ValueError: bad"
    assert canary.describe(KeyError("k")) == "KeyError: 'k'"


def test_stale_detection():
    assert not canary.data_is_stale(7, 7)
    assert not canary.data_is_stale(6, 7)  # one behind is tolerated
    assert canary.data_is_stale(5, 7)
    assert canary.data_is_stale(None, 3)
    assert not canary.data_is_stale(None, 0)  # season not started
    assert not canary.data_is_stale(None, 1)
