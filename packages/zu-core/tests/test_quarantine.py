"""#83 — the quarantined (tool-less, egress-free) run-mode, proved offline.

A quarantined run is the dual-LLM "quarantined reader": the policy is offered an
EMPTY tool set, so a poisoned page can corrupt only the typed facts the reader
returns (a data-integrity problem), never trigger an action (a control-flow one).
A tool call that arrives anyway is the content trying to escape — refused, with a
high-signal ``harness.quarantine.escape_attempt`` event and run-level taint, not a
silent no-op. These tests assert that containment is STRUCTURAL (the flag changes
behaviour mechanically), not by convention.
"""

from __future__ import annotations

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.contracts import Status, TaskSpec
from zu_core.loop import run_task
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider


class RecordingTool:
    """A tool whose body records every call, so we can prove it never ran."""

    name = "fetch"
    tier = 1
    schema = {"name": "fetch", "parameters": {"type": "object", "properties": {}}}
    prompt_fragment = "fetch()"
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self.calls: list = []

    async def __call__(self, ctx, **kw) -> dict:
        self.calls.append(kw)
        return {"content": "secret page body"}


def _capturing_provider(moves: list[dict]) -> tuple[ScriptedProvider, list[list[dict]]]:
    """A ScriptedProvider that also records the tool schemas it was OFFERED each
    turn — so a test can assert the menu was empty under quarantine."""
    offered: list[list[dict]] = []
    provider = ScriptedProvider.from_moves(moves)
    orig = provider.complete

    async def complete(req):
        offered.append(list(req.tools))
        return await orig(req)

    provider.complete = complete  # type: ignore[method-assign]
    return provider, offered


async def test_quarantined_run_offers_no_tools_and_refuses_a_tool_call() -> None:
    tool = RecordingTool()
    reg = Registry()
    reg.register("tools", "fetch", tool)
    provider, offered = _capturing_provider(
        [{"tool": "fetch", "args": {}}, {"text": '{"read": true}', "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="read the page", quarantined=True), provider, reg, bus)

    # The run completes, but the tool NEVER executed — the call was refused.
    assert result.status == Status.SUCCESS
    assert tool.calls == []
    # The menu the model saw was empty: egress is denied because the only path
    # off-box (a tool) was never offered.
    assert offered and all(menu == [] for menu in offered)

    types = [e.type for e in await bus.query()]
    # The attempt is SURFACED as a high-signal event, and taint is raised.
    assert ev.QUARANTINE_ESCAPE_ATTEMPT in types
    assert ev.TAINT_RAISED in types
    escape = next(e for e in await bus.query() if e.type == ev.QUARANTINE_ESCAPE_ATTEMPT)
    assert escape.payload["tool"] == "fetch"
    # The model got an error observation marking the block (a data-integrity signal).
    returned = next(
        e for e in await bus.query()
        if e.type == ev.TOOL_RETURNED and e.payload["tool"] == "fetch"
    )
    assert returned.payload["observation"]["blocked"] == "quarantine_escape"


async def test_same_tool_runs_when_NOT_quarantined() -> None:
    # The control: the identical setup without the flag executes the tool — so it
    # is the quarantine flag, not the wiring, that contains the call.
    tool = RecordingTool()
    reg = Registry()
    reg.register("tools", "fetch", tool)
    provider = ScriptedProvider.from_moves(
        [{"tool": "fetch", "args": {}}, {"text": '{"read": true}', "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="read the page"), provider, reg, bus)

    assert result.status == Status.SUCCESS
    assert tool.calls == [{}]  # it ran exactly once
    types = [e.type for e in await bus.query()]
    assert ev.QUARANTINE_ESCAPE_ATTEMPT not in types


async def test_quarantined_run_with_no_tool_calls_is_clean() -> None:
    # A well-behaved quarantined reader that just reads and returns typed facts
    # raises no escape signal and no taint.
    reg = Registry()
    provider = ScriptedProvider.from_moves([{"text": '{"price": 1299}', "finish": "stop"}])
    bus = EventBus()
    result = await run_task(TaskSpec(query="extract price", quarantined=True), provider, reg, bus)

    assert result.status == Status.SUCCESS
    types = [e.type for e in await bus.query()]
    assert ev.QUARANTINE_ESCAPE_ATTEMPT not in types
    assert ev.TAINT_RAISED not in types
