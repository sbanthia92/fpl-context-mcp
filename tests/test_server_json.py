"""Guards for server.json, which the MCP Registry validates at publish time."""

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_description_fits_registry_limit():
    """The registry rejects descriptions over 100 characters (422 at publish time)."""
    data = json.loads((ROOT / "server.json").read_text())
    assert len(data["description"]) <= 100


def test_versions_agree_with_pyproject():
    data = json.loads((ROOT / "server.json").read_text())
    pyproject = (ROOT / "pyproject.toml").read_text()
    version = next(line for line in pyproject.splitlines() if line.startswith("version"))
    expected = version.split('"')[1]
    assert data["version"] == expected
    assert data["packages"][0]["version"] == expected
