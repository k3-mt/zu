"""F71: floating third-party deps carry a version bound.

A dependency written bare (``"typer"``, ``"httpx"``) has no floor, so a fresh
install can silently resolve it to an incompatible OLD release — the class of
install break that only shows on a real PyPI resolve, never under ``uv sync``
(which pins from the lock). This is the sibling of the #42 extras-pin guard:
fully offline ($0, no model, no network), it parses the named ``pyproject.toml``
files with stdlib ``tomllib`` and asserts each policed dep declares a version
constraint. Generic by construction — it checks *that a bound exists*, not a
specific version, so it stays correct across future bumps.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

# packages/zu-cli/tests/ -> packages/
_PACKAGES_DIR = Path(__file__).resolve().parents[2]

# "typer>=0.12" -> name="typer", spec=">=0.12"; "httpx" -> name="httpx", spec=""
_REQ = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*(?P<spec>.*)$")
# Any PEP 440 version operator counts as a bound (a floor is enough).
_HAS_BOUND = re.compile(r"(>=|>|==|~=|<=|<|!=)")

# The unbounded-floating deps #65/F71 calls out, per package that declares them.
_POLICED: dict[str, set[str]] = {
    "zu-cli": {"typer", "pyyaml"},
    "zu-tools": {"httpx"},
    "zu-testing": {"httpx"},
}


def _dependencies(pyproject: Path) -> list[str]:
    project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
    return list(project.get("dependencies", []))


def test_named_deps_carry_a_version_bound() -> None:
    unbounded: list[str] = []
    checked = 0
    for pkg, deps in _POLICED.items():
        pyproject = _PACKAGES_DIR / pkg / "pyproject.toml"
        assert pyproject.is_file(), f"missing {pyproject}"
        found = {}
        for req in _dependencies(pyproject):
            m = _REQ.match(req.strip())
            if not m:
                continue
            found[m.group("name").lower()] = m.group("spec").strip()
        for dep in deps:
            assert dep in found, f"{pkg}: expected to declare dependency {dep!r}"
            checked += 1
            if not _HAS_BOUND.search(found[dep]):
                unbounded.append(f"{pkg}: {dep!r} has no version bound (got {found[dep]!r})")

    assert checked, "expected to police at least one dependency"
    assert not unbounded, "unbounded floating deps:\n  " + "\n  ".join(unbounded)
