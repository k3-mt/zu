"""Workspace sibling pins stay in lockstep with each package's own version.

Regression guard for the install-breaking class where an optional extra in
`packages/zu/pyproject.toml` pinned a `zu-*` sibling at a stale version while the
base pinned the current one — an unsatisfiable graph on a real PyPI install that
`uv sync` masks (workspace sources never exercise the pin).

This is fully offline ($0, no model, no network): it parses every
`packages/*/pyproject.toml` with stdlib `tomllib` and asserts that every `==`
constraint referencing an in-workspace `zu-*` sibling equals that sibling's own
declared `version`. Generic by construction — it tracks the real version map, so
it stays correct across future version bumps instead of hardcoding a number.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

# packages/zu/tests/ -> packages/
_PACKAGES_DIR = Path(__file__).resolve().parents[2]

# "zu-providers[anthropic]==0.8.0" -> name="zu-providers", spec="==0.8.0"
_REQ = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*(?P<spec>.*)$")
_PIN = re.compile(r"==\s*(?P<version>[0-9][0-9A-Za-z.+!-]*)")


def _load(pyproject: Path) -> dict:
    return tomllib.loads(pyproject.read_text(encoding="utf-8"))


def _version_map() -> dict[str, str]:
    """Map every workspace package's distribution name -> its declared version."""
    versions: dict[str, str] = {}
    for pyproject in _PACKAGES_DIR.glob("*/pyproject.toml"):
        project = _load(pyproject).get("project", {})
        name = project.get("name")
        version = project.get("version")
        if name and version:
            versions[name] = version
    return versions


def _iter_requirements(project: dict):
    """Yield (source_label, requirement_string) for base + every extra."""
    for req in project.get("dependencies", []):
        yield "dependencies", req
    for extra, reqs in project.get("optional-dependencies", {}).items():
        for req in reqs:
            yield f"optional-dependencies.{extra}", req


def test_workspace_sibling_pins_match_their_own_version() -> None:
    versions = _version_map()
    assert "zu-runtime" in versions, "expected to discover the zu-runtime package"

    mismatches: list[str] = []
    checked = 0
    for pyproject in sorted(_PACKAGES_DIR.glob("*/pyproject.toml")):
        project = _load(pyproject).get("project", {})
        owner = project.get("name", pyproject.parent.name)
        for source, req in _iter_requirements(project):
            m = _REQ.match(req.strip())
            if not m:
                continue
            name = m.group("name")
            if name not in versions:  # only police in-workspace siblings
                continue
            pin = _PIN.search(m.group("spec"))
            if not pin:  # sibling referenced without an exact pin — out of scope
                continue
            checked += 1
            pinned = pin.group("version")
            expected = versions[name]
            if pinned != expected:
                mismatches.append(
                    f"{owner} [{source}] pins {name}=={pinned} but {name} is version {expected}"
                )

    assert checked, "expected to police at least one inter-workspace sibling pin"
    assert not mismatches, "stale sibling version pins:\n  " + "\n  ".join(mismatches)
