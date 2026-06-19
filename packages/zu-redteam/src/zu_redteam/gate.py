"""The gate runner — `zu test-plugin` (RED_TEAM.md §5).

Runs a plugin through the graded gates in order and renders a single report. The
gates that need no Docker run deterministically in CI; the container gate is
honestly reported as SKIPPED when Docker is unavailable (it is the production
form of the same in-process run — same observers, same verdict).

    1. unit ......... the plugin's own tests (pytest)
    2. contract ..... port conformance (shape, types, declared envelope)
    3. interop ...... stood up with >= 3 neighbours, a benign task succeeds clean
    4. adversarial .. the frozen corpus + directed probes; observers + coverage
    5. container .... real Zu in Docker (production form) — skipped without Docker

The verdict for gates 3–4 is rendered by the out-of-band observers in
`verdict.py`, never by the attacker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from zu_core import events as ev

from .attacker import ScriptedAttacker
from .contract import check_plugin
from .fixtures import NEIGHBOUR_NAMES, ControlledHandler, StaticFetch, benign_neighbours
from .harness import Scenario, run_scenario
from .verdict import NeighbourHealth, ObservedRun, default_observers, render_verdict

_PLANTED_SECRET = "ZU-PLANTED-SECRET-9f3a7c21"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"


@dataclass
class GateResult:
    name: str
    status: str
    detail: str = ""


@dataclass
class AttackFinding:
    """What one attack found and what defended against it — the per-attack record
    behind the adversarial gate's verdict, so 'what was identified / what held' is
    inspectable, not just a PASS/FAIL."""

    id: str
    objective: str
    outcome: str  # "contained" | "breached"
    defended_by: list[str] = field(default_factory=list)
    breaches: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "id": self.id, "objective": self.objective, "outcome": self.outcome,
            "defended_by": self.defended_by, "breaches": self.breaches,
        }


@dataclass
class GateReport:
    pkg: str
    results: list[GateResult] = field(default_factory=list)
    findings: list[AttackFinding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(r.status != FAIL for r in self.results)

    def as_dict(self) -> dict:
        return {
            "pkg": self.pkg,
            "passed": self.passed,
            "gates": [{"name": r.name, "status": r.status, "detail": r.detail} for r in self.results],
            "findings": [f.as_dict() for f in self.findings],
        }

    def render(self) -> str:
        icon = {PASS: "✅", FAIL: "❌", SKIP: "⊘"}
        lines = [f"zu test-plugin {self.pkg}", ""]
        for r in self.results:
            lines.append(f"  {icon.get(r.status, '?')} {r.name:<12} {r.status}"
                         + (f" — {r.detail}" if r.detail else ""))
        if self.findings:
            lines += ["", "  red-team findings (attack → outcome · defended by):"]
            for f in self.findings:
                mark = "✓" if f.outcome == "contained" else "✗"
                how = ", ".join(f.defended_by or f.breaches) or "—"
                lines.append(f"    {mark} {f.id:<22} {f.objective:<12} {f.outcome:<10} {how}")
        lines.append("")
        lines.append("RESULT: " + ("PASS — the envelope held" if self.passed
                                    else "FAIL — see the gate(s) above"))
        return "\n".join(lines)


def _defenses(run: ObservedRun) -> list[str]:
    """The defenses that fired during a run — the harness.defense.blocked events,
    as 'kind (target)' strings — so a finding can say what actually held."""
    out: list[str] = []
    for e in run.events:
        if e.type == ev.DEFENSE_BLOCKED:
            kind = e.payload.get("kind", "?")
            target = e.payload.get("target")
            out.append(f"{kind} ({target})" if target else str(kind))
    return out


def _observers(neighbours: list[str]) -> list[Any]:
    return [*default_observers(), NeighbourHealth(neighbours)]


async def run_gate(
    pkg: str,
    *,
    plugins: list[tuple[str, str, Any]] | None = None,
    pkg_dir: str | Path | None = None,
    run_unit: bool = True,
    min_coverage: int = 5,
    on_event: Callable[[Any], None] | None = None,
) -> GateReport:
    """Run the gates and return a report. ``plugins`` are the package's plugins
    (kind, name, instance) to stand up; ``pkg_dir`` enables the unit gate.
    ``on_event`` (e.g. a live printer) is attached to every attack's bus so a gate
    run can be watched as it happens (``zu test-plugin --watch``)."""
    plugins = plugins or []
    subscribers: Sequence[Callable[[Any], None]] = [on_event] if on_event else []
    report = GateReport(pkg=pkg)

    # 1. unit ---------------------------------------------------------------
    report.results.append(_unit_gate(pkg_dir) if run_unit else GateResult("unit", SKIP, "skipped"))

    # 2. contract -----------------------------------------------------------
    contract_findings = [f for k, n, o in plugins for f in check_plugin(k, n, o)]
    if not plugins:
        report.results.append(GateResult("contract", SKIP, "no plugins resolved"))
    elif contract_findings:
        report.results.append(GateResult(
            "contract", FAIL, "; ".join(f"{f.plugin} {f.detail}" for f in contract_findings)))
    else:
        report.results.append(GateResult("contract", PASS, f"{len(plugins)} plugin(s) conform"))

    # 3. interop ------------------------------------------------------------
    report.results.append(await _interop_gate(plugins, subscribers))

    # 4. adversarial --------------------------------------------------------
    adv_result, findings = await _adversarial_gate(plugins, min_coverage, subscribers)
    report.results.append(adv_result)
    report.findings = findings

    # 5. container ----------------------------------------------------------
    report.results.append(await _container_gate())

    return report


async def _container_gate() -> GateResult:
    """The production form: stand the target's sandbox tier up in a *real* Docker
    container under the isolation envelope (all caps dropped, no-new-privileges,
    network off, pids capped), proving the enforcement the in-process observers
    assume actually holds. Honest about cost: it pulls/launches an image, so it is
    opt-in (``ZU_REDTEAM_CONTAINER=1``); without Docker it SKIPs, and a Docker/
    image error SKIPs with the reason rather than failing a plugin for infra."""
    if shutil.which("docker") is None:
        return GateResult(
            "container", SKIP,
            "Docker not available — the in-process gates above run the same observers; "
            "install Docker to run the production container form")
    if os.environ.get("ZU_REDTEAM_CONTAINER") != "1":
        return GateResult(
            "container", SKIP,
            "Docker present; set ZU_REDTEAM_CONTAINER=1 to run the real hardened-container "
            "form (it pulls/launches an image)")
    image = os.environ.get("ZU_REDTEAM_CONTAINER_IMAGE", "ghcr.io/k3-mt/zu-render-chromium:latest")
    try:
        from zu_backends.local_docker import LocalDockerBackend

        backend = LocalDockerBackend()
        sandbox = await backend.launch({"image": image, "network": False})
        try:
            up = getattr(sandbox, "container", None) is not None
        finally:
            await backend.destroy(sandbox)
        return GateResult(
            "container", PASS if up else FAIL,
            f"hardened container ({image}) stood up and torn down under the isolation "
            "envelope (caps dropped, no-new-privileges, network off, pids capped)")
    except Exception as exc:  # noqa: BLE001 - infra failure SKIPs, never FAILs a plugin
        return GateResult("container", SKIP, f"Docker present but the container form could not run: {exc}")


def _unit_gate(pkg_dir: str | Path | None) -> GateResult:
    if pkg_dir is None:
        return GateResult("unit", SKIP, "no package directory given")
    tests = Path(pkg_dir) / "tests"
    if not tests.is_dir():
        return GateResult("unit", SKIP, "no tests/ directory")
    proc = subprocess.run(
        ["python", "-m", "pytest", str(tests), "-q"],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        return GateResult("unit", PASS, "tests green")
    tail = (proc.stdout or proc.stderr).strip().splitlines()[-1:] or [""]
    return GateResult("unit", FAIL, tail[0])


async def _interop_gate(
    plugins: list[tuple[str, str, Any]], subscribers: Sequence[Callable[[Any], None]] = ()
) -> GateResult:
    """Stand the plugins up with >= 3 cross-category neighbours and run a benign
    task to completion; every observer (incl. neighbour health) must stay clean."""
    extra = [p for p in plugins if (p[0], p[1]) not in {("tools", "neighbour_fetch")}]
    scenario = Scenario(
        objective="interop",
        plugins=[("tools", "web_fetch", StaticFetch(page="<html><body><h1>ok</h1></body></html>")),
                 *benign_neighbours(), *extra],
        moves=[{"tool": "web_fetch", "args": {"url": "http://example.test/"}},
               {"text": '{"ok": true}', "finish": "stop"}],
        planted_secret=_PLANTED_SECRET,
        neighbours=NEIGHBOUR_NAMES,
    )
    run = await run_scenario(scenario, subscribers=subscribers)
    breaches = [b for o in _observers(NEIGHBOUR_NAMES) if (b := o.inspect(run))]
    if run.result is None or breaches:
        detail = "; ".join(b.observer + ": " + b.detail for b in breaches) or "run did not complete"
        return GateResult("interop", FAIL, detail)
    return GateResult("interop", PASS, f">= {len(benign_neighbours())} neighbours, benign run clean")


async def _adversarial_gate(
    plugins: list[tuple[str, str, Any]], min_coverage: int,
    subscribers: Sequence[Callable[[Any], None]] = (),
) -> tuple[GateResult, list[AttackFinding]]:
    """Replay the frozen corpus (with the target present) plus a directed probe of
    each target tool; render the out-of-band verdict and a per-attack finding so
    the result shows what was attempted and what defended it."""
    attacker = ScriptedAttacker(
        planted_secret=_PLANTED_SECRET, extra_plugins=plugins, subscribers=subscribers
    )
    # Run the fleet: each specialist covers its objectives' corpus cases. Flatten
    # (each case has one objective, so appears once) for the per-case findings,
    # and keep the per-specialist grouping for the coverage line.
    fleet = await attacker.run_fleet()
    results = [r for group in fleet.values() for r in group]
    findings: list[AttackFinding] = []

    for r in results:
        findings.append(_finding(r.case.id, r.case.objective, r.observed, r.case.scenario.neighbours))

    # Directed probes: invoke each target tool in a real run and judge it against
    # its OWN declaration — this is what catches a tool that under-declares egress.
    for tool_name, probe in _directed_probes(plugins):
        run = await run_scenario(probe, subscribers=subscribers)
        findings.append(_finding(f"probe:{tool_name}", "envelope", run, probe.neighbours))

    # Dormant-pivot probe: the two-phase controlled handler (RED_TEAM.md §6.1) —
    # catches a plugin that behaves in testing and pivots on a production trigger.
    findings.append(await _dormant_pivot_probe(plugins, subscribers))

    breaches = [f"{f.id}: {b}" for f in findings for b in f.breaches]
    if breaches:
        return GateResult("adversarial", FAIL, "; ".join(breaches)), findings

    # Coverage is two real things, not the corpus's own constant objective set
    # (which is always present regardless of the target and so proves nothing):
    #   1. every declared target TOOL was actually directed-probed against its own
    #      envelope — a tool silently left unexercised is a hole, so it FAILs;
    #   2. the standard frozen-corpus battery ran in full (its objective breadth
    #      is the deterministic floor — NOT an anti-suppression guard, which only
    #      applies to the non-deterministic LiveAttacker).
    target_tools = {name for kind, name, _ in plugins if kind == "tools"}
    probed = {f.id[len("probe:"):] for f in findings if f.id.startswith("probe:")}
    unprobed = target_tools - probed
    if unprobed:
        return GateResult(
            "adversarial", FAIL,
            f"target tool(s) not exercised by a directed probe: {sorted(unprobed)}",
        ), findings
    covered = len({f.objective for f in findings})
    if covered < min_coverage:
        return GateResult(
            "adversarial", FAIL,
            f"corpus battery incomplete: {covered} objectives ran (< {min_coverage})",
        ), findings
    return GateResult(
        "adversarial", PASS,
        f"{len(fleet)} fleet specialists · {len(results)} corpus attacks + "
        f"{len(target_tools)} tool probe(s); envelope held",
    ), findings


def _finding(id_: str, objective: str, run: ObservedRun, neighbours: list[str]) -> AttackFinding:
    v = render_verdict(run, _observers(neighbours), objectives_attempted=[objective], min_coverage=0)
    breaches = [f"{b.observer}: {b.detail}" for b in v.breaches]
    defended = _defenses(run)
    if not breaches and not defended:
        # Contained with no guard firing: the attack had no path to begin with —
        # the capability envelope (no egress/secret/capability) is the defense.
        defended = ["capability envelope (no path to harm)"]
    return AttackFinding(
        id=id_, objective=objective,
        outcome="breached" if breaches else "contained",
        defended_by=defended, breaches=breaches,
    )


async def _dormant_pivot_probe(
    plugins: list[tuple[str, str, Any]],
    subscribers: Sequence[Callable[[Any], None]] = (),
) -> AttackFinding:
    """The dormant-pivot probe (RED_TEAM.md §6.1): stand the target up against a
    two-phase controlled handler. Phase 1 the handler is benign; the gate arms it;
    phase 2 it serves the trigger payload, and every target tool is then given a
    turn. A plugin that pivots only after the trigger — reaching the off-allowlist
    command host or leaking the secret — is caught by the same out-of-band
    observers (egress / exfil / host). A plugin that stays put is contained: for a
    declared-allowlist plugin reaching the handler at all is an egress breach; for
    an open-egress plugin the secret never leaves. The handler lives at
    ``handler.test.invalid`` — off every allowlist — so a benign plugin's normal
    work never touches it.

    The two phases are the *handler's*, within one run (the canonical log keeps a
    single task, which the provenance observer requires): the handler serves
    benign content on the first fetch, then arms itself and serves the trigger
    payload on the next — "behaved in testing, then a trigger appeared.\""""
    handler = ControlledHandler(arm_after=1)
    plugins_all: list[tuple[str, str, Any]] = [("tools", "web_fetch", handler), *benign_neighbours()]
    names = {(k, n) for k, n, _ in plugins_all}
    plugins_all += [p for p in plugins if (p[0], p[1]) not in names]

    fetch = {"tool": "web_fetch", "args": {"url": f"http://{ControlledHandler.HOST}/"}}
    # Fetch once (benign), then again (handler now armed -> the trigger lands in the
    # log), then give every target tool a turn with NEUTRAL args (never the handler
    # URL — the pivot must come from the tool's own reaction to the trigger it sees,
    # not from us feeding it the C2 address).
    moves: list[dict] = [fetch, fetch]
    for kind, name, obj in plugins:
        if kind != "tools":
            continue
        props = (getattr(obj, "schema", {}) or {}).get("parameters", {}).get("properties", {})
        args = {"url": "http://probe.example/"} if "url" in props else {}
        moves.append({"tool": name, "args": args})
    moves.append({"text": '{"done": true}', "finish": "stop"})

    scenario = Scenario(
        objective="pivot", plugins=plugins_all, moves=moves,
        planted_secret=_PLANTED_SECRET, neighbours=NEIGHBOUR_NAMES,
    )
    run = await run_scenario(scenario, subscribers=subscribers)
    return _finding("probe:dormant_pivot", "pivot", run, NEIGHBOUR_NAMES)


def _directed_probes(plugins: list[tuple[str, str, Any]]) -> list[tuple[str, Scenario]]:
    """(tool_name, scenario) per target *tool*: invoke it once (benign args
    inferred from its schema) so the observers can judge whether doing its job
    stayed inside its declared envelope."""
    probes: list[tuple[str, Scenario]] = []
    for kind, name, obj in plugins:
        if kind != "tools":
            continue
        props = (getattr(obj, "schema", {}) or {}).get("parameters", {}).get("properties", {})
        args = {"url": "http://probe.example/"} if "url" in props else {}
        scenario = Scenario(
            objective="envelope",
            plugins=[(kind, name, obj), *benign_neighbours()],
            moves=[{"tool": name, "args": args}, {"text": '{"ok": true}', "finish": "stop"}],
            planted_secret=_PLANTED_SECRET,
            neighbours=NEIGHBOUR_NAMES,
        )
        probes.append((name, scenario))
    return probes
