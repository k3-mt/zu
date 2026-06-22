"""The meta-agent construction driver — the diagnose → edit → rebuild loop.

The headline of the construction sequence: capture a site once, then iterate the agent
OFFLINE and free until it builds clean AND clears the anti-hardcode guardrails — reading
each round's diagnosis to decide the next edit. This module is the loop's SKELETON: the
orchestration is real and fully exercised offline with a scripted strategist; the two
inherently-live parts are explicit ``NotImplementedError`` seams.

* The **strategist** decides the next edit from a diagnosis. ``ScriptedStrategist`` replays
  a fixed list (tests, and a deterministic offline demo); ``LiveStrategist`` is the seam —
  a model deciding edits, the next increment.
* **Live capture** (stage 2) is the seam ``live_capture``; ``construct`` takes an already
  captured bundle, exactly as ``zu capture`` produces.

The driver NEVER promotes (guardrail G4): it returns a bundle + report for review. Reuses
``build.build_offline`` (the offline spine) and ``guardrails.enforce_guardrails`` (the
gate) — no new offline machinery.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .build import BuildReport, build_offline
from .guardrails import GuardrailReport, enforce_guardrails
from .offline import Bundle


@dataclass
class Edit:
    """A strategist's proposed change: the mutated bundle to try next, and why."""

    bundle: Bundle
    note: str


@dataclass
class Diagnosis:
    """What a strategist sees at a failing round — enough to decide the next edit."""

    round: int
    build: BuildReport
    guardrails: GuardrailReport
    bundle: Bundle


@runtime_checkable
class Strategist(Protocol):
    """Decides the next edit from a diagnosis, or ``None`` to give up."""

    async def propose(self, diagnosis: Diagnosis) -> Edit | None: ...


@dataclass
class ScriptedStrategist:
    """Replays a fixed list of edits, one per failing round — the deterministic driver for
    tests and an offline demo. Returns ``None`` once the script is exhausted."""

    edits: list[Edit]
    _i: int = 0

    async def propose(self, diagnosis: Diagnosis) -> Edit | None:
        if self._i >= len(self.edits):
            return None
        edit = self.edits[self._i]
        self._i += 1
        return edit


class LiveStrategist:
    """The seam: a model reads the diagnosis and proposes the next edit. Not built here —
    it needs a frontier model (and, in the headline design, a Claude CLI driving the
    ``zu mcp`` tools inside ``zu run --sandboxed``). The next increment."""

    async def propose(self, diagnosis: Diagnosis) -> Edit | None:
        raise NotImplementedError(
            "the live strategist is the live lane — it needs a model to decide the next "
            "edit (the headline meta-agent: a Claude CLI driving the zu mcp tools in a "
            "sandbox). Inject a ScriptedStrategist for offline runs, or use "
            "`zu construct --check` for a one-round readiness report."
        )


def live_capture(spec: Any, cfg: Any, agent_dir: str | Path) -> Bundle:
    """The seam: stage-2 live capture (drive the site once, project a bundle). Not built
    here — it needs keys + network. Use ``zu capture`` to produce ``fixtures/capture.json``
    first; ``construct`` then iterates it offline."""
    raise NotImplementedError(
        "live capture needs keys + network — run `zu capture <agent>` once to record "
        "fixtures/capture.json, then construct iterates it offline."
    )


@dataclass
class RoundResult:
    round: int
    build_ok: bool
    guardrails_passed: bool
    note: str


@dataclass
class ConstructionReport:
    rounds: list[RoundResult] = field(default_factory=list)
    final_build: BuildReport | None = None
    final_guardrails: GuardrailReport | None = None
    bundle: Bundle | None = None   # the working bundle as last tried — handed back for review

    @property
    def converged(self) -> bool:
        return bool(self.final_build and self.final_build.ok
                    and self.final_guardrails and self.final_guardrails.passed)


async def construct(
    spec: Any, cfg: Any, agent_dir: str | Path, bundle: Bundle, strategist: Strategist,
    *, max_rounds: int = 3, min_resilience: float = 1.0,
) -> ConstructionReport:
    """Iterate the agent offline until it builds clean and clears the guardrails, or the
    strategist gives up / ``max_rounds`` is hit. Each round: build the offline spine, then
    enforce the anti-hardcode gate; on a hold, ask the strategist for an edit and retry
    with the mutated bundle. Never promotes (G4) — returns the bundle + report for review."""
    report = ConstructionReport(bundle=bundle)
    for r in range(1, max_rounds + 1):
        build = await build_offline(spec, cfg, agent_dir, bundle, min_score=min_resilience)
        guards = await enforce_guardrails(
            spec, cfg, bundle, agent_dir, min_resilience=min_resilience)
        report.final_build = build
        report.final_guardrails = guards
        report.bundle = bundle

        if build.ok and guards.passed:
            report.rounds.append(RoundResult(r, True, True, "converged"))
            return report

        held = ("build held" if not build.ok else "") + (
            ("; " if not build.ok and not guards.passed else "")
            + (f"{len(guards.violations)} guardrail violation(s)" if not guards.passed else ""))
        edit = await strategist.propose(Diagnosis(r, build, guards, bundle))
        if edit is None:
            report.rounds.append(RoundResult(r, build.ok, guards.passed, f"{held}; gave up"))
            return report
        report.rounds.append(RoundResult(r, build.ok, guards.passed, f"{held}; edit: {edit.note}"))
        bundle = edit.bundle

    # Ran out of rounds — record where the last attempt stood (already on the report).
    return report
