"""Stand up a target in a real Zu run and capture it for the verdict observers.

The gate attacks the plugin running in **real Zu**, not a mock (PHILOSOPHY.md §3):
a scenario registers the target plus its interop neighbours, drives the run with a
deterministic (scripted) victim policy, and the run's canonical event log becomes
the :class:`ObservedRun` the out-of-band observers judge. Nothing here decides
pass/fail — it only produces the record the judge reads.

The deterministic, CI-runnable form runs in-process (the production escalation is
the same run inside a Docker container; the observers are identical because they
read the same event log). Live frontier-model discovery is the opt-in escalation,
not this path.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from zu_core.bus import EventBus
from zu_core.contracts import Budget, TaskSpec
from zu_core.loop import run_task
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider

from .verdict import ObservedRun

# A plugin to register, as (kind, name, instance) — the shape Registry.register
# takes, so a scenario can mix tools, detectors, and validators freely.
Plugin = tuple[str, str, Any]


@dataclass
class Scenario:
    """One self-contained attack run: the plugins in the box, the victim policy's
    scripted moves, the task, and the planted secret to watch for exfiltration."""

    objective: str
    plugins: list[Plugin]
    moves: list[dict]
    query: str = "Extract the requested data."
    target: str | None = None
    planted_secret: str = ""
    budget: Budget = field(default_factory=Budget)
    # The neighbour tool names whose health the gate should check.
    neighbours: list[str] = field(default_factory=list)


async def run_scenario(
    scenario: Scenario, *, subscribers: Sequence[Callable[[Any], None]] = ()
) -> ObservedRun:
    """Run one scenario on real Zu in-process and return the out-of-band view.

    ``subscribers`` are attached to the run's bus before it starts — how
    ``zu test-plugin --watch`` streams each attack live as it happens."""
    registry = Registry()
    for kind, name, obj in scenario.plugins:
        registry.register(kind, name, obj)

    provider = ScriptedProvider.from_moves(scenario.moves)
    bus = EventBus()
    for sub in subscribers:
        bus.subscribe(sub)
    spec = TaskSpec(query=scenario.query, target=scenario.target, budget=scenario.budget)
    result = await run_task(spec, provider, registry, bus)
    events = await bus.query()
    return ObservedRun.from_events(
        events, result, planted_secret=scenario.planted_secret, budget=spec.budget
    )
