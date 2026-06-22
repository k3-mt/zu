"""Anti-hardcode guardrails — the executable gate on autonomous construction output.

The design's meta-agent is only safe if its output is held to concrete, load-bearing
rules: a generic, resilient agent, never one that memorised the answer. This module makes
those rules executable, reusing the stage-5 machinery (``harden.audit_brittleness`` and
``harden.harden``):

* **G1 — every targeting step has an alternate locator.** A click/fill/select with no
  ``near`` fallback is a single point of failure (one renamed selector breaks it).
* **G2 — the track is resilient.** It clears a resilience threshold AND grounding is
  load-bearing (value-deletion controls fail), so the score is real.
* **G3 — no literal site-answer constant baked in.** None of the captured answer's
  grounded values may appear verbatim in ``agent.yaml`` or a bundle tool's source — the
  "never `click Chislehurst`, never emit the answer as a constant" rule. A generic agent
  DERIVES those values; it must not ship them.
* **G4 — review gate** is structural, enforced by the driver (``construct``): the output
  is a bundle + report handed back for sign-off, never auto-promoted.

This gate is intentionally STRICTER than ``zu build``: ``zu build`` *notes* single-selector
brittleness (a hand-authored minimal example legitimately has one); the guardrails *fail*
on it, because they gate autonomous output bound for production.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .harden import audit_brittleness, grounded_values, harden
from .offline import Bundle


@dataclass(frozen=True)
class GuardrailViolation:
    """One failed guardrail — the gate's reason to hold the output for rework."""

    rule: str     # "single-selector" | "resilience" | "hardcoded-answer"
    detail: str


@dataclass
class GuardrailReport:
    violations: list[GuardrailViolation] = field(default_factory=list)
    resilience: float = 1.0

    @property
    def passed(self) -> bool:
        return not self.violations


def _config_text(agent_dir: str | Path) -> str:
    """The agent's authored surface a hardcoded answer could hide in: agent.yaml plus any
    bundle tool source. Read best-effort — a missing tools/ dir is fine."""
    base = Path(agent_dir)
    parts: list[str] = []
    for name in ("agent.yaml", "agent.yml"):
        p = base / name
        if p.is_file():
            parts.append(p.read_text(encoding="utf-8"))
    tools = base / "tools"
    if tools.is_dir():
        for py in sorted(tools.rglob("*.py")):
            try:
                parts.append(py.read_text(encoding="utf-8"))
            except OSError:
                continue
    return "\n".join(parts)


async def enforce_guardrails(
    spec: Any, cfg: Any, bundle: Bundle, agent_dir: str | Path, *, min_resilience: float = 1.0,
) -> GuardrailReport:
    """Apply G1–G3 to a captured bundle and return the violations (empty == pass). Pure
    $0: the resilience check replays perturbations offline; no model, no network."""
    violations: list[GuardrailViolation] = []

    # G1 — alternate locators: every single-selector finding is a violation.
    for f in audit_brittleness(bundle):
        if f.kind == "single-selector":
            violations.append(GuardrailViolation("single-selector", f"{f.where}: {f.detail}"))

    # G2 — resilience: clears the threshold AND grounding actually gates.
    hr = await harden(spec, cfg, bundle)
    if not hr.grounding_load_bearing:
        violations.append(GuardrailViolation(
            "resilience", "a value-deletion control passed — grounding is not gating, so "
            "the resilience score is unreliable"))
    elif hr.resilience < min_resilience:
        violations.append(GuardrailViolation(
            "resilience", f"resilience {hr.resilience:.0%} below required {min_resilience:.0%}"))

    # G3 — no hardcoded answer: a grounded value verbatim in config/tool source.
    text = _config_text(agent_dir)
    for value in grounded_values(bundle):
        if value in text:
            violations.append(GuardrailViolation(
                "hardcoded-answer", f"the grounded value {value!r} appears verbatim in the "
                "agent config or a tool's source — a generic agent must derive it, not "
                "hardcode it"))

    return GuardrailReport(violations=violations, resilience=hr.resilience)
