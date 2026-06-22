"""The meta-agent construction driver — the diagnose → edit → rebuild loop.

The headline of the construction sequence: capture a site once, then iterate the agent
OFFLINE and free until it builds clean AND clears the anti-hardcode guardrails — reading
each round's diagnosis to decide the next edit. The orchestration is real and fully
exercised offline; the one inherently-live part (capturing a site) stays a seam.

* The **strategist** decides the next edit from a diagnosis. ``ScriptedStrategist`` replays
  a fixed list (tests, and a deterministic offline demo); ``LiveStrategist`` asks a model —
  given a provider it hardens the single-selector steps (adds a ``near`` alternate locator
  drawn from the captured page text); given none it stays a seam, so ``zu construct``
  without a live model still stops cleanly.
* **Live capture** (stage 2) is the seam ``live_capture``; ``construct`` takes an already
  captured bundle, exactly as ``zu capture`` produces.

The driver NEVER promotes (guardrail G4): it returns a bundle + report for review. Reuses
``build.build_offline`` (the offline spine) and ``guardrails.enforce_guardrails`` (the
gate) — no new offline machinery.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from zu_core.ports import ModelProvider, ModelRequest

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


# --- the live strategist: a model proposes the next edit ---------------------

_TARGETING = ("click", "fill", "select")


def _brittle_steps(bundle: Bundle) -> list[tuple[int, int, str, Any]]:
    """The targeting actions in the bundle's moves that lack a ``near`` fallback — the
    single-selector steps an alternate locator would harden. Returns each as
    ``(move_index, action_index, verb, selector)`` so an edit can patch it precisely (the
    structural counterpart to ``harden.audit_brittleness``, which only reports)."""
    steps: list[tuple[int, int, str, Any]] = []
    for mi, move in enumerate(bundle.moves):
        if move.get("tool") not in ("browser", "render_dom"):
            continue
        for ai, action in enumerate(move.get("args", {}).get("actions") or []):
            if not isinstance(action, dict):
                continue
            verb = next((v for v in _TARGETING if v in action), None)
            if verb and "near" not in action:
                steps.append((mi, ai, verb, action[verb]))
    return steps


def _page_text(bundle: Bundle, *, limit: int = 2000) -> str:
    """The visible text the captured browser/render observations showed — the context the
    model draws a real on-page label from when choosing a ``near`` anchor."""
    parts: list[str] = []
    for tool in ("browser", "render_dom"):
        for obs in bundle.observations.get(tool, []):
            t = obs.get("text") or obs.get("html") or ""
            if isinstance(t, str) and t.strip():
                parts.append(t.strip())
    return "\n".join(parts)[:limit]


def _balanced_spans(text: str) -> list[str]:
    """Balanced ``{...}`` / ``[...]`` runs in ``text`` — to recover JSON a model wrapped in
    prose. String/escape-aware, so a brace inside a quoted value doesn't fool the scan."""
    spans: list[str] = []
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        depth = 0
        start = -1
        in_str = False
        esc = False
        for i, ch in enumerate(text):
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == open_ch:
                if depth == 0:
                    start = i
                depth += 1
            elif ch == close_ch and depth:
                depth -= 1
                if depth == 0 and start >= 0:
                    spans.append(text[start : i + 1])
    return spans


def _extract_json(text: str | None) -> Any:
    """Best-effort parse of a model reply into JSON: the whole text, a fenced ```json
    block, or the first balanced array/object embedded in prose (models prepend a
    sentence). Returns ``None`` if nothing parses — the caller then gives up cleanly."""
    if not text:
        return None
    import json
    import re

    candidates = [text]
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    candidates.extend(_balanced_spans(text))
    for c in candidates:
        try:
            return json.loads(c)
        except (ValueError, TypeError):
            continue
    return None


def _parse_fixes(data: Any, n_steps: int) -> dict[int, str]:
    """Normalise the model's reply into ``{step_index: near_label}`` — accepting a bare list
    or a ``{"fixes": [...]}`` wrapper, and dropping anything out of range or malformed (so a
    sloppy reply yields fewer fixes, never a crash)."""
    items = data.get("fixes") if isinstance(data, dict) else data
    out: dict[int, str] = {}
    if not isinstance(items, list):
        return out
    for item in items:
        if not isinstance(item, dict):
            continue
        step, near = item.get("step"), item.get("near")
        if (isinstance(step, int) and 0 <= step < n_steps
                and isinstance(near, str) and near.strip()):
            out[step] = near.strip()
    return out


def _edit_messages(
    diagnosis: Diagnosis, steps: list[tuple[int, int, str, Any]], page_text: str
) -> list[dict]:
    """The prompt: the task, why the round was held, the numbered brittle steps, and the
    page text to anchor against — asking for STRICT JSON mapping each step to a ``near``
    label. Deliberately generic: it asks for a nearby VISIBLE label, never a site answer."""
    violations = "\n".join(
        f"- [{v.rule}] {v.detail}" for v in diagnosis.guardrails.violations) or "- (none)"
    listed = "\n".join(
        f"  step {i}: a `{verb}` targeting {selector!r} with no `near` fallback"
        for i, (_mi, _ai, verb, selector) in enumerate(steps))
    system = (
        "You harden a browser-automation path. A targeting step that relies on a single "
        "selector breaks when the site renames it; adding a `near` anchor (a short, stable "
        "VISIBLE label beside the control) lets the runtime resolve the control by "
        "proximity as a fallback. Choose anchors from the page text only — never invent a "
        "value, and never encode the task's answer. Reply with STRICT JSON and nothing else."
    )
    user = (
        f"Task: {diagnosis.bundle.task}\n\n"
        f"This construction round was held:\n{violations}\n\n"
        f"Single-selector steps to harden:\n{listed}\n\n"
        f"Visible page text (choose `near` anchors from here):\n{page_text}\n\n"
        'Reply with JSON: {"fixes": [{"step": <int>, "near": "<short visible label>"}]}. '
        "Include only the steps you can anchor; omit any you cannot."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


class LiveStrategist:
    """A model reads the diagnosis and proposes the next edit — the live lane of the loop.
    Given a ``provider`` it asks the model to harden the single-selector steps (adding a
    ``near`` alternate locator drawn from the captured page text) and applies the reply to a
    fresh bundle. Constructed WITHOUT a provider it stays a seam (``NotImplementedError``),
    so ``zu construct`` without a live model still stops cleanly.

    Scope of this increment: it fixes G1 (single-selector) brittleness — what a *bundle*
    edit can address. A G3 hardcoded answer lives in the agent config, not the bundle, so
    this strategist cannot patch it via an Edit; it returns ``None`` (gives up) and leaves
    that for review (G4). The headline form — a Claude CLI driving the ``zu mcp`` tools in
    ``zu run --sandboxed``, free to edit agent.yaml too — is the next step out from here."""

    def __init__(self, provider: ModelProvider | None = None) -> None:
        self._provider = provider

    async def propose(self, diagnosis: Diagnosis) -> Edit | None:
        if self._provider is None:
            raise NotImplementedError(
                "the live strategist is the live lane — it needs a model to decide the next "
                "edit (the headline meta-agent: a Claude CLI driving the zu mcp tools in a "
                "sandbox). Pass a provider, inject a ScriptedStrategist for offline runs, or "
                "use `zu construct --check` for a one-round readiness report."
            )
        steps = _brittle_steps(diagnosis.bundle)
        if not steps:
            # The only holds are things a bundle edit can't fix — a G3 hardcoded answer in
            # the config, or a build failure — so give up and leave them for review (G4).
            return None
        req = ModelRequest(
            messages=_edit_messages(diagnosis, steps, _page_text(diagnosis.bundle)))
        resp = await self._provider.complete(req)
        fixes = _parse_fixes(_extract_json(resp.text), len(steps))
        if not fixes:
            return None
        patched = copy.deepcopy(diagnosis.bundle)
        applied: list[str] = []
        for idx, near in fixes.items():
            mi, ai, verb, selector = steps[idx]
            patched.moves[mi]["args"]["actions"][ai]["near"] = near
            applied.append(f"{verb} {selector!r} +near={near!r}")
        return Edit(bundle=patched, note="add `near` fallback(s): " + "; ".join(applied))


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
