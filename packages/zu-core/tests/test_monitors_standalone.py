"""#38 — the standalone, loop-free monitor fold (``zu_core.monitors``).

``run_monitors`` folds a list of Monitors over an arbitrary event sequence and reduces
to the worst MonitorVerdict, with NO RunContext built by the caller and NO event
emitted. ``evaluate_invariants`` compiles declared invariants down the same path. A
final equivalence test drives the SAME monitor + events through ``run_task`` and asserts
the loop's emitted verdict matches ``run_monitors`` over the captured log — the
regression guard for the loop's ``_monitor_checkpoint`` refactor onto this one fold.
"""

from __future__ import annotations

from test_monitor import (
    Echo,
    ForbiddenToolMonitor,
    WarnMonitor,
)

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.contracts import Status, TaskSpec
from zu_core.invariants import (
    Invariant,
    InvariantKind,
    Predicate,
    PredicateKind,
    compile_spec,
)
from zu_core.loop import run_task
from zu_core.monitors import evaluate_invariants, run_monitors
from zu_core.ports import MonitorState, RunContext
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider


class _Ev:
    """A minimal event double: the monitors read only ``.type`` and ``.payload``."""

    def __init__(self, type_: str, payload: dict) -> None:
        self.type = type_
        self.payload = payload


def _events_with_tool(tool: str = "echo") -> list[_Ev]:
    return [_Ev(ev.TOOL_INVOKED, {"tool": tool}), _Ev(ev.TOOL_RETURNED, {})]


def test_run_monitors_returns_worst_verdict() -> None:
    warn = WarnMonitor()
    violation = ForbiddenToolMonitor("echo")
    events = _events_with_tool("echo")
    # WARN + VIOLATION over the same log → the worst (VIOLATION) wins.
    worst = run_monitors([warn, violation], events)
    assert worst is not None
    assert worst.state == MonitorState.VIOLATION
    assert worst.monitor == "no_forbidden_tool"
    # Order-independent: same worst regardless of monitor order.
    assert run_monitors([violation, warn], events) == worst


def test_run_monitors_warn_only() -> None:
    events = _events_with_tool("echo")
    worst = run_monitors([WarnMonitor()], events)
    assert worst is not None
    assert worst.state == MonitorState.WARN
    assert worst.monitor == "warn_on_tool"


def test_run_monitors_empty_list_is_none() -> None:
    assert run_monitors([], _events_with_tool("echo")) is None


def test_run_monitors_clean_monitor_is_none() -> None:
    # A monitor that never fires over this log (no forbidden tool present).
    clean = ForbiddenToolMonitor("forbidden_never_called")
    assert run_monitors([clean], _events_with_tool("echo")) is None


def test_run_monitors_builds_no_context_and_emits_nothing() -> None:
    # The caller passes a raw event sequence and a bare spec; it builds NO RunContext
    # and there is NO bus, so no event can be emitted — the helper is pure.
    events = _events_with_tool("echo")
    before = list(events)
    worst = run_monitors([ForbiddenToolMonitor("echo")], events, spec=None)
    assert worst is not None and worst.state == MonitorState.VIOLATION
    # The input sequence is untouched (no event appended).
    assert events == before


def test_evaluate_invariants_matches_manual_compile_path() -> None:
    inv = Invariant(
        name="cap_two_tools",
        kind=InvariantKind.THROUGHOUT,
        predicate=Predicate(
            kind=PredicateKind.BUDGET_CAP, params={"metric": "tool_calls", "limit": 1}
        ),
    )
    # Two tool invocations over the cap of 1 → the compiled invariant fires VIOLATION.
    events = [_Ev(ev.TOOL_INVOKED, {"tool": "x"}), _Ev(ev.TOOL_INVOKED, {"tool": "x"})]
    via_helper = evaluate_invariants([inv], events)
    via_manual = compile_spec([inv])[0].evaluate(RunContext(spec=None, events=events))
    assert via_helper == via_manual
    assert via_helper is not None and via_helper.state == MonitorState.VIOLATION


async def test_loop_verdict_equals_standalone_over_captured_log() -> None:
    """Drive the SAME monitor + events through ``run_task`` and assert the loop's
    emitted MONITOR_FIRED matches ``run_monitors`` folded over the captured log — the
    regression guard that ``_monitor_checkpoint`` now delegates to the one fold."""
    reg = Registry()
    reg.register("tools", "echo", Echo())
    reg.register("monitors", "no_forbidden_tool", ForbiddenToolMonitor("echo"))
    provider = ScriptedProvider.from_moves(
        [{"tool": "echo", "args": {}}, {"text": '{"ok": true}', "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert result.status == Status.TERMINAL

    captured = await bus.query()
    fired = [e for e in captured if e.type == ev.MONITOR_FIRED]
    assert fired, "the loop emitted a monitor.fired"

    standalone = run_monitors([ForbiddenToolMonitor("echo")], captured)
    assert standalone is not None
    # The loop emitted exactly the verdict the standalone fold computes over its log.
    assert standalone.monitor == fired[-1].payload["monitor"]
    assert standalone.state.value == fired[-1].payload["state"]
