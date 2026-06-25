"""ZU-RAIL-6 — invariants declared as DATA compile down to Monitors.

The pure predicate evaluators are tested over hand-built event lists (no loop, no
model); the compiled-invariant-in-loop test reuses the ZU-RAIL-5 monitor wiring to
prove a declared budget cap halts the run when overshot.
"""

from __future__ import annotations

from uuid import uuid4

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.contracts import Event, Status, TaskSpec
from zu_core.invariants import (
    Invariant,
    InvariantKind,
    Predicate,
    PredicateKind,
    compile_invariant,
    predicate_holds,
)
from zu_core.loop import run_task
from zu_core.ports import Monitor, MonitorState, RunContext
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider

_TRACE = uuid4()
_TASK = uuid4()


def _ev(type_: str, payload: dict) -> Event:
    return Event(trace_id=_TRACE, task_id=_TASK, type=type_, source="test", payload=payload)


def _tool_invoked(tool: str) -> Event:
    return _ev(ev.TOOL_INVOKED, {"tool": tool})


def test_budget_cap_predicate_holds_then_breaks() -> None:
    pred = Predicate(kind=PredicateKind.BUDGET_CAP, params={"metric": "tool_calls", "limit": 2})
    two = [_tool_invoked("x"), _tool_invoked("x")]
    three = two + [_tool_invoked("x")]
    assert predicate_holds(pred, two) is True  # at the limit
    assert predicate_holds(pred, three) is False  # over the limit


def test_domain_allowlist_flags_off_allowlist_destination() -> None:
    pred = Predicate(
        kind=PredicateKind.DOMAIN_ALLOWLIST,
        params={"event_type": ev.SOURCE_FETCHED, "field": "ctx.destination",
                "allow": ["api.good.test"]},
    )
    ok = [_ev(ev.SOURCE_FETCHED, {"ctx": {"destination": "api.good.test"}})]
    bad = [_ev(ev.SOURCE_FETCHED, {"ctx": {"destination": "evil.test"}})]
    assert predicate_holds(pred, ok) is True
    assert predicate_holds(pred, bad) is False


def test_required_field_presence() -> None:
    pred = Predicate(
        kind=PredicateKind.REQUIRED_FIELD,
        params={"event_type": ev.RECORD_EXTRACTED, "field": "value"},
    )
    present = [_ev(ev.RECORD_EXTRACTED, {"value": 42})]
    missing = [_ev(ev.RECORD_EXTRACTED, {})]
    assert predicate_holds(pred, present) is True
    assert predicate_holds(pred, missing) is False


def test_compile_invariant_yields_a_monitor() -> None:
    inv = Invariant(
        name="cap_tools",
        kind=InvariantKind.THROUGHOUT,
        predicate=Predicate(kind=PredicateKind.BUDGET_CAP, params={"metric": "tool_calls", "limit": 1}),
    )
    monitor = compile_invariant(inv)
    assert isinstance(monitor, Monitor)  # runtime_checkable structural match

    holding = RunContext(spec=None, events=[_tool_invoked("x")])
    breaking = RunContext(spec=None, events=[_tool_invoked("x"), _tool_invoked("x")])
    assert monitor.evaluate(holding) is None  # within cap
    v = monitor.evaluate(breaking)
    assert v is not None and v.state == MonitorState.VIOLATION


class Echo:
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


async def test_compiled_invariant_escalates_in_loop() -> None:
    # A declared budget cap of 1 tool call, compiled to a Monitor, registered and
    # run with a provider that overshoots to 2 calls => VIOLATION => TERMINAL.
    inv = Invariant(
        name="tool_budget",
        kind=InvariantKind.THROUGHOUT,
        predicate=Predicate(kind=PredicateKind.BUDGET_CAP, params={"metric": "tool_calls", "limit": 1}),
    )
    reg = Registry()
    reg.register("tools", "echo", Echo())
    reg.register("monitors", "tool_budget", compile_invariant(inv))
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
    assert result.reason == "tool_budget"
    fired = [e for e in await bus.query() if e.type == ev.MONITOR_FIRED]
    assert fired and fired[0].payload["state"] == "violation"
