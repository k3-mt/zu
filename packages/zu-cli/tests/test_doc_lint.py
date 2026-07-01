"""Doc-lint guards: the shipped docs stay honest and complete ($0, offline).

These assert on the text of the top-level ``README.md`` and ``AGENTS.md`` so a
regression in what we *claim* is caught like any other test:

* F80 — the README no longer overstates the live escalation demo / tier-2 status
  (the live path currently raises).
* O6 — AGENTS.md is honest that ``Policy`` is a code-level seam, not an
  ``agent.yaml`` block.
* F79 — the AGENTS.md repository map lists every shipped ``packages/zu-*`` dir.
"""

from __future__ import annotations

import re
from pathlib import Path

# packages/zu-cli/tests/ -> repo root
_ROOT = Path(__file__).resolve().parents[3]
_README = _ROOT / "README.md"
_AGENTS = _ROOT / "AGENTS.md"
_PACKAGES = _ROOT / "packages"


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# --- F80: the README does not overstate live escalation / tier-2 completeness ---


def test_readme_does_not_claim_live_escalation_is_real_today() -> None:
    text = _README.read_text(encoding="utf-8")
    # The overstated heading is gone (it implied the live arc runs today).
    assert "The five-minute promise (real today)" not in text
    # The overstated invitation to run the *escalation* demo live is gone.
    assert "watch a real model\nmake the same escalation decision" not in text
    assert "watch a real model make the same escalation decision" not in text
    # And the doc is honest that the live tier-2 path is not complete / raises.
    lower = text.lower()
    assert "raises" in lower and "tier-2" in lower
    assert "not complete" in lower or "not available" in lower or "offline only" in lower


# --- O6: Policy is documented as a code-level seam, not an agent.yaml block ---


def test_agents_md_is_honest_policy_is_not_an_agent_yaml_block() -> None:
    text = _AGENTS.read_text(encoding="utf-8")
    assert "code-level" in text
    # It explicitly says there is no policy: key (whitespace-insensitive so a line
    # wrap between "no" and "`policy:`" doesn't break the assertion).
    normalized = " ".join(text.split())
    assert "no `policy:` key" in normalized
    assert "not an `agent.yaml` block" in normalized


# --- F79: the AGENTS.md repo map lists every packages/zu-* dir ---


def _repo_map_block(text: str) -> str:
    """The first fenced code block after the 'Repository layout' heading."""
    after = text.split("## Repository layout", 1)[1]
    m = re.search(r"```(.*?)```", after, re.DOTALL)
    assert m, "expected a fenced repo-map code block after 'Repository layout'"
    return m.group(1)


def test_agents_repo_map_lists_every_shipped_package() -> None:
    block = _repo_map_block(_AGENTS.read_text(encoding="utf-8"))
    dirs = sorted(p.name for p in _PACKAGES.iterdir() if p.is_dir() and p.name.startswith("zu"))
    missing = [d for d in dirs if f"{d}/" not in block]
    assert not missing, f"AGENTS.md repo map omits packages: {missing}"
    # Regression anchors for the two F79 called out.
    assert "zu-patterns/" in block
    assert "zu-shadow/" in block


def test_readme_repo_map_lists_the_two_previously_omitted_packages() -> None:
    text = _README.read_text(encoding="utf-8")
    assert "zu-patterns/" in text
    assert "zu-shadow/" in text
