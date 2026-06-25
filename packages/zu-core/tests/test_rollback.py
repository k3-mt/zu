"""ZU-RAIL-8 — restore-to-last-known-good rollback.

Builds on the EXISTING event-sourcing (_rebuild_run_state / _resume_from_log): it
folds ONLY the good prefix of a prior log (dropping the failed tail) to re-seat a
run at a last-known-good (LKG) event, then re-enters the model loop for a DIFFERENT
on-rail retry — distinct from forward-resume-from-pause. Consume-once is preserved.

Offline: ScriptedProvider + hand-built / recorded prior logs, no model, no network.
"""

from __future__ import annotations

from uuid import uuid4

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.contracts import Event, Status, TaskSpec
from zu_core.loop import (
    _rebuild_run_state,
    _rebuild_to,
    last_known_good,
    rollback_and_replan,
)
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider

_TRACE = uuid4()
_TASK = uuid4()


def _ev(type_: str, payload: dict, *, event_id=None) -> Event:
    kw = {"event_id": event_id} if event_id is not None else {}
    return Event(trace_id=_TRACE, task_id=_TASK, type=type_, source="test", payload=payload, **kw)


def _started() -> Event:
    return _ev(ev.TASK_STARTED, {"query": "q", "tainted": False})


def test_last_known_good_picks_latest_marker() -> None:
    m1 = _ev(ev.CHECKPOINT_MARKED, {"label": "a", "step": 1})
    m2 = _ev(ev.CHECKPOINT_MARKED, {"label": "b", "step": 3})
    log = [_started(), m1, _ev(ev.TOOL_INVOKED, {"tool": "x"}), m2, _ev(ev.TOOL_INVOKED, {"tool": "y"})]
    assert last_known_good(log) == m2.event_id  # the LATER marker wins


def test_last_known_good_falls_back_to_last_returned() -> None:
    r1 = _ev(ev.TOOL_RETURNED, {"tool": "x"})
    r2 = _ev(ev.TOOL_RETURNED, {"tool": "y"})
    log = [_started(), _ev(ev.TOOL_INVOKED, {"tool": "x"}), r1, _ev(ev.TOOL_INVOKED, {"tool": "y"}), r2]
    assert last_known_good(log) == r2.event_id  # no marker => latest good return


def test_rebuild_to_drops_failed_tail() -> None:
    # A prior log that climbs the tier then fails; an earlier LKG must yield the
    # EARLIER tier, proving the failed tail was dropped (differs from the full fold).
    start = _started()
    marker = _ev(ev.CHECKPOINT_MARKED, {"label": "good", "step": 2})
    climb = _ev(ev.TASK_ESCALATED, {"from_tier": 1, "to_tier": 2})
    log = [start, _ev(ev.TOOL_INVOKED, {"tool": "x"}), marker, climb, _ev(ev.TASK_TERMINAL, {"reason": "boom"})]

    full = _rebuild_run_state(log)
    prefix = _rebuild_to(log, marker.event_id)
    assert full["tier"] == 2  # the full fold sees the climb in the failed tail
    assert prefix["tier"] == 1  # the good-prefix fold stops at the marker, before the climb


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


async def _record_prior(reg: Registry) -> list[Event]:
    """Run a task to one tool call, then hand-append a checkpoint + a failed tail —
    a realistic prior log to roll back from."""
    provider = ScriptedProvider.from_moves(
        [{"tool": "echo", "args": {"good": 1}}, {"text": '{"ok": true}', "finish": "stop"}]
    )
    bus = EventBus()
    await run_task_local(reg, provider, bus)
    log = list(await bus.query())
    # A consumer marks the good point after the first tool returned, then the run
    # later went off-rail (a failed tail we will drop).
    log.append(_ev(ev.CHECKPOINT_MARKED, {"label": "after_echo", "step": len(log)}))
    log.append(_ev(ev.TOOL_INVOKED, {"tool": "echo"}))
    log.append(_ev(ev.TASK_TERMINAL, {"reason": "off_rail"}))
    return log


async def run_task_local(reg: Registry, provider, bus: EventBus) -> None:
    from zu_core.loop import run_task

    await run_task(TaskSpec(query="q"), provider, reg, bus, trace_id=_TRACE)


async def test_rollback_restores_state_and_replans() -> None:
    reg = Registry()
    reg.register("tools", "echo", Echo())
    prior = await _record_prior(reg)

    # A FRESH tool to observe the re-plan: the model picks a different arg.
    target = Echo()
    reg2 = Registry()
    reg2.register("tools", "echo", target)
    new_provider = ScriptedProvider.from_moves(
        [{"tool": "echo", "args": {"different": 99}}, {"text": '{"ok": true}', "finish": "stop"}]
    )
    bus = EventBus()
    result = await rollback_and_replan(
        TaskSpec(query="q"), new_provider, prior=prior, registry=reg2, bus=bus, trace_id=_TRACE,
    )
    assert result.status == Status.SUCCESS
    events = await bus.query()
    rolled = [e for e in events if e.type == ev.RUN_ROLLED_BACK]
    assert rolled, "rollback must emit harness.run.rolled_back"
    assert rolled[0].payload["dropped"] == 2  # the appended tool.invoked + task.terminal
    # The model re-planned: the new (different) tool call executed.
    assert target.calls == [{"different": 99}]


async def test_rollback_preserves_consume_once() -> None:
    from zu_core.ledger import InMemoryExecutionLedger

    reg = Registry()
    reg.register("tools", "echo", Echo())
    # A prior log whose GOOD PREFIX already claimed a key, plus a claim ONLY in the
    # dropped failed tail.
    start = _started()
    marker = _ev(ev.CHECKPOINT_MARKED, {"label": "g", "step": 2})
    good_claim = _ev(ev.EXECUTION_CLAIMED, {"key": "side-effect-A"})
    prior = [
        start,
        _ev(ev.TOOL_INVOKED, {"tool": "echo"}),
        good_claim,
        marker,
        _ev(ev.EXECUTION_CLAIMED, {"key": "tail-only-B"}),  # in the dropped tail
        _ev(ev.TASK_TERMINAL, {"reason": "boom"}),
    ]
    ledger = InMemoryExecutionLedger()
    provider = ScriptedProvider.from_moves([{"text": '{"ok": true}', "finish": "stop"}])
    bus = EventBus()
    await rollback_and_replan(
        TaskSpec(query="q"), provider, prior=prior, to=marker.event_id,
        registry=reg, bus=bus, ledger=ledger, trace_id=_TRACE,
    )
    # The good-prefix claim is re-loaded => re-claiming it is REFUSED (not re-run).
    assert ledger.claim("side-effect-A") is False
    # The dropped-tail claim is gone => its key is free again (the tail's effect
    # was never committed to the good prefix).
    assert ledger.claim("tail-only-B") is True


async def test_rollback_honors_per_tier_provider() -> None:
    # The good prefix climbed to tier 2; the rollback re-seats the run at that tier.
    # A per-tier ``providers`` override for tier 2 must drive the re-plan — proving
    # the same model-loop kwargs a normal run_task accepts are threaded through.
    tier2_tool = Echo()
    tier2_tool.tier = 2
    reg = Registry()
    reg.register("tools", "echo", tier2_tool)

    start = _started()
    climb = _ev(ev.TASK_ESCALATED, {"from_tier": 1, "to_tier": 2})
    marker = _ev(ev.CHECKPOINT_MARKED, {"label": "g", "step": 2})
    prior = [
        start,
        climb,
        _ev(ev.TOOL_INVOKED, {"tool": "echo"}),
        marker,
        _ev(ev.TASK_TERMINAL, {"reason": "boom"}),
    ]

    # The GLOBAL provider would refuse to call the tool (asserts if used); only the
    # per-tier (tier-2) provider drives the replan, so its move must be the one taken.
    global_provider = ScriptedProvider.from_moves([{"text": "should-not-run", "finish": "stop"}])
    tier2_provider = ScriptedProvider.from_moves(
        [{"tool": "echo", "args": {"replan": 7}}, {"text": '{"ok": true}', "finish": "stop"}]
    )
    bus = EventBus()
    result = await rollback_and_replan(
        TaskSpec(query="q", max_tier=2), global_provider, prior=prior, to=marker.event_id,
        registry=reg, bus=bus, providers={2: tier2_provider}, trace_id=_TRACE,
    )
    assert result.status == Status.SUCCESS
    # The tier-2 provider's move executed — the per-tier override was honored.
    assert tier2_tool.calls == [{"replan": 7}]


async def test_rollback_differs_from_forward_resume() -> None:
    # The same prior log: the full fold (forward-resume basis) sees the failed-tail
    # tier climb; the rollback fold (good prefix only) does not.
    start = _started()
    marker = _ev(ev.CHECKPOINT_MARKED, {"label": "g", "step": 1})
    climb = _ev(ev.TASK_ESCALATED, {"from_tier": 1, "to_tier": 3})
    prior = [start, marker, climb, _ev(ev.TASK_TERMINAL, {"reason": "boom"})]
    full = _rebuild_run_state(prior)  # what forward-resume folds (whole log)
    prefix = _rebuild_to(prior, marker.event_id)  # what rollback folds (good prefix)
    assert full["tier"] == 3
    assert prefix["tier"] == 1
    assert full["tier"] != prefix["tier"]
