"""ZU-RAIL-5 — the stateful, history-aware Monitor over the event stream.

A Monitor is the stateful generalisation of a Detector: it folds the WHOLE event
history via ``ctx.events`` and returns OK/WARN/VIOLATION. A VIOLATION wires into
the SAME escalation control flow detectors use (Verdict/Severity/halting path):
it maps to a TERMINAL Verdict and ends the run. Pure — no model, no network.
"""

from __future__ import annotations

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.contracts import Status, TaskSpec
from zu_core.loop import run_task
from zu_core.ports import MonitorState, MonitorVerdict, RunContext
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider


class Echo:
    """An inert tier-1 tool that records its calls; emits a tool.returned event."""

    name = "echo"
    tier = 1
    schema = {"name": "echo", "parameters": {"type": "object", "properties": {}}}
    prompt_fragment = "echo()"
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self.calls: list = []

    async def __call__(self, ctx, **kw) -> dict:
        self.calls.append(kw)
        return {"text": "ok"}


class ForbiddenToolMonitor:
    """VIOLATION once a forbidden tool's invocation appears anywhere on the log."""

    name = "no_forbidden_tool"

    def __init__(self, forbidden: str = "echo") -> None:
        self._forbidden = forbidden

    def evaluate(self, ctx: RunContext) -> MonitorVerdict | None:
        for i, e in enumerate(ctx.events):
            if e.type == ev.TOOL_INVOKED and e.payload.get("tool") == self._forbidden:
                return MonitorVerdict(
                    monitor=self.name, state=MonitorState.VIOLATION,
                    detail=f"{self._forbidden} is forbidden", step=i,
                )
        return None


class WarnMonitor:
    """WARN once any tool has been invoked — records but never halts."""

    name = "warn_on_tool"

    def evaluate(self, ctx: RunContext) -> MonitorVerdict | None:
        if any(e.type == ev.TOOL_INVOKED for e in ctx.events):
            return MonitorVerdict(monitor=self.name, state=MonitorState.WARN, detail="a tool ran")
        return None


class SecondHitMonitor:
    """VIOLATION only after the SECOND tool.returned — proves it folds a stream,
    not a single observation."""

    name = "second_hit"

    def evaluate(self, ctx: RunContext) -> MonitorVerdict | None:
        returns = [e for e in ctx.events if e.type == ev.TOOL_RETURNED]
        if len(returns) >= 2:
            return MonitorVerdict(
                monitor=self.name, state=MonitorState.VIOLATION, detail="second return",
            )
        return None


class BrokenMonitor:
    name = "boom"

    def evaluate(self, ctx: RunContext) -> MonitorVerdict | None:
        raise RuntimeError("monitor blew up")


async def _type_sequence(bus: EventBus) -> list[str]:
    return [e.type for e in await bus.query()]


async def test_monitor_violation_escalates_to_terminal() -> None:
    tool = Echo()
    reg = Registry()
    reg.register("tools", "echo", tool)
    reg.register("monitors", "no_forbidden_tool", ForbiddenToolMonitor("echo"))
    provider = ScriptedProvider.from_moves(
        [{"tool": "echo", "args": {}}, {"text": '{"ok": true}', "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert result.status == Status.TERMINAL
    assert result.reason == "no_forbidden_tool"
    events = await bus.query()
    fired = [e for e in events if e.type == ev.MONITOR_FIRED]
    assert fired and fired[0].payload["state"] == "violation"
    assert fired[0].payload["monitor"] == "no_forbidden_tool"


async def test_monitor_warn_records_and_continues() -> None:
    tool = Echo()
    reg = Registry()
    reg.register("tools", "echo", tool)
    reg.register("monitors", "warn_on_tool", WarnMonitor())
    provider = ScriptedProvider.from_moves(
        [{"tool": "echo", "args": {}}, {"text": '{"ok": true}', "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert result.status == Status.SUCCESS  # WARN does not halt
    fired = [e for e in await bus.query() if e.type == ev.MONITOR_FIRED]
    assert fired and fired[0].payload["state"] == "warn"


async def test_no_monitor_registered_is_inert() -> None:
    # Baseline: no monitors registered.
    def build() -> tuple[Registry, ScriptedProvider]:
        reg = Registry()
        reg.register("tools", "echo", Echo())
        provider = ScriptedProvider.from_moves(
            [{"tool": "echo", "args": {}}, {"text": '{"ok": true}', "finish": "stop"}]
        )
        return reg, provider

    reg_a, prov_a = build()
    bus_a = EventBus()
    await run_task(TaskSpec(query="q"), prov_a, reg_a, bus_a)
    baseline = await _type_sequence(bus_a)

    # Identical run, still no monitors: byte-identical event-type sequence.
    reg_b, prov_b = build()
    bus_b = EventBus()
    await run_task(TaskSpec(query="q"), prov_b, reg_b, bus_b)
    assert await _type_sequence(bus_b) == baseline
    assert ev.MONITOR_FIRED not in baseline


async def test_monitor_folds_whole_history() -> None:
    tool = Echo()
    reg = Registry()
    reg.register("tools", "echo", tool)
    reg.register("monitors", "second_hit", SecondHitMonitor())
    # Two tool calls: the monitor must NOT fire on the first return, only the second.
    provider = ScriptedProvider.from_moves(
        [
            {"tool": "echo", "args": {"n": 1}},
            {"tool": "echo", "args": {"n": 2}},
            {"text": '{"ok": true}', "finish": "stop"},
        ]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert result.status == Status.TERMINAL
    assert result.reason == "second_hit"
    # Exactly one echo executed before the second-return violation halted the run.
    returns = [e for e in await bus.query() if e.type == ev.TOOL_RETURNED]
    assert len(returns) == 2


async def test_broken_monitor_is_isolated() -> None:
    tool = Echo()
    reg = Registry()
    reg.register("tools", "echo", tool)
    reg.register("monitors", "boom", BrokenMonitor())
    provider = ScriptedProvider.from_moves(
        [{"tool": "echo", "args": {}}, {"text": '{"ok": true}', "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    # A raising monitor is logged + skipped; the run completes normally.
    assert result.status == Status.SUCCESS
    assert ev.MONITOR_FIRED not in await _type_sequence(bus)
