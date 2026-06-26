"""`zu capabilities` dumps the reconciled surface (issue #30) — human + JSON. $0."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from zu_cli.main import app


def test_capabilities_human_lists_kinds_and_library_packages() -> None:
    result = CliRunner().invoke(app, ["capabilities"])
    assert result.exit_code == 0
    out = result.stdout
    assert "capability surface" in out
    assert "PLUGIN KINDS" in out and "providers" in out and "patterns" in out
    # the import-only headline packages are surfaced too.
    assert "LIBRARY PACKAGES" in out
    assert "zu-shadow" in out and "zu-patterns" in out
    assert "fsm_from_events" in out  # the cross-run memory layer, by name


def test_capabilities_json_is_machine_readable() -> None:
    result = CliRunner().invoke(app, ["capabilities", "--json"])
    assert result.exit_code == 0
    doc = json.loads(result.stdout)
    assert doc["provenance"]["version"]
    kinds = {k["kind"] for k in doc["kinds"]}
    assert {"providers", "tools", "patterns"} <= kinds
    assert any(lib["dist"] == "zu-shadow" for lib in doc["library"])
