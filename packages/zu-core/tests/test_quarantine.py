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

import pytest

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.contracts import Status, TaskSpec
from zu_core.grants import InMemoryGrantStore
from zu_core.ledger import InMemoryExecutionLedger
from zu_core.loop import run_task
from zu_core.ports import CAP_NET, EGRESS_OPEN
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


class NetTool:
    """A tier-1 tool that declares off-box reach (net + open egress). Under a
    ``containment="required"`` posture this is exactly the kind of tool that, if
    REACHABLE, would refuse a non-sandboxed run. A quarantined reader never offers
    it, so the effective tool set is empty and containment is a no-op."""

    name = "fetch"
    tier = 1
    schema = {"name": "fetch", "parameters": {"type": "object", "properties": {}}}
    prompt_fragment = "fetch()"
    capabilities: frozenset[str] = frozenset({CAP_NET})
    egress: frozenset[str] = frozenset({EGRESS_OPEN})

    async def __call__(self, ctx, **kw) -> dict:
        return {"content": "secret page body"}


async def test_quarantine_denies_egress_and_isolates_state() -> None:
    # (a) A net/egress tool under containment="required" does NOT refuse a
    # quarantined run (the effective tool set is empty), and the declared envelope
    # reports zero egress + the additive quarantined marker — NOT the net tool's
    # egress. On the OLD code, enforce_containment over the full set raises, and the
    # envelope lists fetch's open egress: this proof fails on current behaviour.
    reg = Registry()
    reg.register("tools", "fetch", NetTool())
    provider = ScriptedProvider.from_moves([{"text": '{"read": true}', "finish": "stop"}])
    bus = EventBus()

    # No ContainmentRequired is raised even though containment="required" and the
    # registered tool declares off-box reach: the quarantined reader offers it none.
    result = await run_task(
        TaskSpec(query="read the page", quarantined=True),
        provider, reg, bus, containment="required",
    )
    assert result.status == Status.SUCCESS

    declared = next(e for e in await bus.query() if e.type == ev.ENVELOPE_DECLARED)
    # The audit log states ZERO egress: the effective tool set is empty and the
    # additive marker records WHY (a structurally tool-less run).
    assert declared.payload["tools"] == {}
    assert declared.payload["quarantined"] is True

    # (b) Memory/store isolation is asserted, not assumed: pairing quarantined=True
    # with a shared grant store (or ledger) fails loud. On the OLD code this is
    # silently accepted — the reader would share durable state across the trust
    # boundary.
    with pytest.raises(ValueError, match="isolation is part of the contract"):
        await run_task(
            TaskSpec(query="read the page", quarantined=True),
            ScriptedProvider.from_moves([{"text": "{}", "finish": "stop"}]),
            Registry(),
            EventBus(),
            grants=InMemoryGrantStore(),
        )

    with pytest.raises(ValueError, match="isolation is part of the contract"):
        await run_task(
            TaskSpec(query="read the page", quarantined=True),
            ScriptedProvider.from_moves([{"text": "{}", "finish": "stop"}]),
            Registry(),
            EventBus(),
            ledger=InMemoryExecutionLedger(),
        )
