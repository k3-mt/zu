"""The container gate: collect → merge → judge on out-of-band evidence.

This is the orchestration for the out-of-band enforcement form (RED_TEAM_CONTAINER.md
§3, phase P0). It closes the gap `RED_TEAM.md` §1 leaves open: in-process, the
egress verdict rests on the URL the *tool reports in its own observation*, so a
deceptive tool that reaches one host and logs another is invisible. Here the
verdict instead rests on facts collected from **outside** the target — the egress
proxy's connection log and the host-effect audit log — which a tool cannot author.

P0 runs the target on real Zu *in-process* (the ``runner``, default
``run_scenario``) and replays the proxy/audit facts a real container would have
produced (a ``ScriptedEgressProxy`` + a host-effect list), so the whole pipeline
runs deterministically with no Docker. P1 swaps the runner and proxy for the real
container + ``LocalEgressProxy`` without changing this file: the merge and the
observers are identical, because they already read the same event log.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from zu_core import events as ev
from zu_core.contracts import Event

from .harness import Scenario, run_scenario
from .runner import jsonl_to_events
from .verdict import Breach, NeighbourHealth, ObservedRun, default_observers

# A runner produces the in-container view of one scenario. ``run_scenario`` (real
# Zu, in-process) is the P0 default; a real container backend is the P1 swap-in.
Runner = Callable[..., Awaitable[ObservedRun]]


def _trace_task_ids(events: list[Event]) -> tuple[Any, Any]:
    """The (trace_id, task_id) the out-of-band facts attach to, taken from the run
    so the merged events share its identity. A run always has events; the uuid
    fallback is only for an empty log (a defensive, never-hit path)."""
    for e in events:
        return e.trace_id, e.task_id
    return uuid4(), uuid4()


def merge_evidence(
    run: ObservedRun,
    connections: Sequence[dict],
    host_effects: Sequence[dict] = (),
) -> ObservedRun:
    """Fold the out-of-band proxy + audit facts into the run as harness-attributed
    ``EGRESS_OBSERVED`` / ``HOST_EFFECT_OBSERVED`` events, then rebuild the
    ``ObservedRun`` the observers judge.

    The facts are emitted with ``source='harness'`` because they come from the
    control plane *outside* the target — a deceptive tool can neither author them
    nor suppress them. They are appended (append-only is preserved) with fresh
    event ids and no parent, so the provenance observer stays clean."""
    trace, task = _trace_task_ids(run.events)
    extra: list[Event] = []
    for c in connections:
        extra.append(Event(trace_id=trace, task_id=task, type=ev.EGRESS_OBSERVED,
                           source="harness", payload=dict(c)))
    for h in host_effects:
        extra.append(Event(trace_id=trace, task_id=task, type=ev.HOST_EFFECT_OBSERVED,
                           source="harness", payload=dict(h)))
    return ObservedRun.from_events(
        list(run.events) + extra, run.result,
        planted_secret=run.planted_secret, budget=run.budget,
    )


@dataclass
class ContainerResult:
    """The container gate's verdict over one run: the merged out-of-band view, the
    breaches the observers found, and the raw evidence behind them."""

    observed: ObservedRun
    breaches: list[Breach]
    connections: list[dict]
    host_effects: list[dict]

    @property
    def passed(self) -> bool:
        return not self.breaches

    def summary(self) -> str:
        if self.passed:
            return (f"contained — {len(self.connections)} egress connection(s) observed "
                    "out of band; envelope held")
        return "BREACH — " + "; ".join(f"{b.observer}: {b.detail}" for b in self.breaches)


def _declared_allowlist(scenario: Scenario) -> list[str]:
    """The union of every target tool's declared egress — what a real proxy would
    enforce, and what the observer judges an observed connection against."""
    allow: set[str] = set()
    for kind, _name, obj in scenario.plugins:
        if kind == "tools":
            allow.update(getattr(obj, "egress", None) or ())
    return sorted(allow)


@dataclass
class ContainerGate:
    """Run a scenario in the container form and judge it on **out-of-band** evidence.

    ``proxy`` is an ``EgressProxy`` (P0: a ``ScriptedEgressProxy`` replaying the
    connection log; P1: the real ``LocalEgressProxy``). ``host_effects`` are the
    host-effect audit facts (P0: a replayed list; P3: a real monitor). ``runner``
    produces the in-container event log (default ``run_scenario`` — real Zu,
    in-process). The observers are the same out-of-band panel the in-process gate
    uses; only their inputs are now authoritative."""

    proxy: Any | None = None
    host_effects: list[dict] = field(default_factory=list)
    runner: Runner | None = None

    async def run(
        self, scenario: Scenario, *, subscribers: Sequence[Callable[[Any], None]] = ()
    ) -> ContainerResult:
        proxy_handle = None
        if self.proxy is not None:
            proxy_handle = await self.proxy.launch({"allowlist": _declared_allowlist(scenario)})
        # The target runs in the box (P0: in-process on real Zu).
        runner = self.runner or run_scenario
        run = await runner(scenario, subscribers=subscribers)
        # Collect the out-of-band evidence.
        connections = self.proxy.connections(proxy_handle) if self.proxy is not None else []
        if self.proxy is not None:
            await self.proxy.close(proxy_handle)
        # Merge and judge with the same observers as in-process — only the inputs
        # are now produced outside the target.
        merged = merge_evidence(run, connections, self.host_effects)
        observers = [*default_observers(), NeighbourHealth(scenario.neighbours)]
        breaches = [b for o in observers if (b := o.inspect(merged)) is not None]
        return ContainerResult(
            observed=merged, breaches=breaches,
            connections=list(connections), host_effects=list(self.host_effects),
        )


@dataclass
class DockerContainerRunner:
    """Run a scenario spec INSIDE a real container behind an egress proxy, then
    judge it on out-of-band evidence (RED_TEAM_CONTAINER.md §3, the P1 live form).

    Backend- and proxy-agnostic by design: pass a live ``LocalDockerBackend`` +
    ``LocalEgressProxy`` in production, or fakes in tests. The flow is exactly the
    one the design describes — launch the proxy → launch the container on the
    internal network with HTTP(S)_PROXY set → exec ``zu-redteam-run`` with the
    spec → read its JSONL event log → collect the proxy log → merge → judge — so
    the whole plumbing is exercised in CI with fakes; only the Docker daemon
    itself is the un-fakeable part the opt-in live run covers.

    The spec is the ``zu_redteam.runner`` form (plugins by import path) and must
    carry ``allowlist`` (the union egress the proxy enforces), ``planted_secret``,
    and ``neighbours`` so the judge has what the in-process gate has."""

    backend: Any           # a SandboxBackend with launch/exec_entrypoint/destroy
    proxy: Any             # an EgressProxy
    image: str
    network_name: str = "zu-redteam-net"
    entrypoint: tuple[str, ...] = ("zu-redteam-run",)
    host_monitor: Any | None = None   # a HostEffectMonitor (P3); None = no fs/proc audit

    async def run(self, spec: dict) -> ContainerResult:
        proxy_handle = await self.proxy.launch({"allowlist": list(spec.get("allowlist") or [])})
        host_effects: list[dict] = []
        sandbox = None
        try:
            launch_spec: dict = {
                "image": self.image,
                "network": "isolated",
                "network_name": self.network_name,
                "proxy": {"host": proxy_handle.host, "port": proxy_handle.port},
            }
            # If the proxy is MITM-enabled (P2), ship its per-run CA so the
            # in-container client trusts the proxy and HTTPS payloads are visible.
            mitm = getattr(self.proxy, "mitm", None)
            if mitm is not None:
                launch_spec["ca_cert"] = mitm.ca_cert_pem()
            sandbox = await self.backend.launch(launch_spec)
            code, out, err = await self.backend.exec_entrypoint(
                sandbox, list(self.entrypoint),
                environment={"ZU_REDTEAM_SPEC": json.dumps(spec)},
            )
            if not out.strip():
                raise RuntimeError(f"in-container runner produced no event log (exit {code}): {err[:300]}")
            events = jsonl_to_events(out)
            # Collect the host-effect audit while the container is still alive
            # (it inspects the live sandbox), before teardown below.
            if self.host_monitor is not None:
                host_effects = await self.host_monitor.collect(sandbox, self.backend)
        finally:
            if sandbox is not None:
                await self.backend.destroy(sandbox)
        run = ObservedRun.from_events(events, None, planted_secret=spec.get("planted_secret", ""))
        connections = self.proxy.connections(proxy_handle)
        await self.proxy.close(proxy_handle)
        merged = merge_evidence(run, connections, host_effects)
        observers = [*default_observers(), NeighbourHealth(spec.get("neighbours") or [])]
        breaches = [b for o in observers if (b := o.inspect(merged)) is not None]
        return ContainerResult(
            observed=merged, breaches=breaches,
            connections=list(connections), host_effects=list(host_effects),
        )
