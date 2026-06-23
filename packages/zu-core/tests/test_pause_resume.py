"""ZU-CD-1/2/5 — human-in-the-loop ESCALATE: pause, approval-binding, resume.

Proves, offline and in two phases:
  * a gate ``kind="human"`` ESCALATE PAUSES the run and the approval record shows
    the LITERAL harness-held invocation parameters (ground truth, not narration);
  * the human resolution is bound to the exact invocation by its idempotency key;
  * resume executes ONLY that invocation, unchanged, exactly once, with the gate,
    taint, and accumulated state preserved;
  * an approval bound to a different key is rejected (approve-then-swap defeated).
"""

from __future__ import annotations

from uuid import uuid4

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.contracts import Event, Status, TaskSpec
from zu_core.loop import run_task
from zu_core.ports import Severity, Verdict
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider


class WireTransfer:
    name = "wire_transfer"
    tier = 1
    schema = {
        "name": "wire_transfer",
        "parameters": {"type": "object", "properties": {"amount": {"type": "number"}}},
    }
    prompt_fragment = "wire_transfer(amount)"
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self.calls: list = []

    async def __call__(self, ctx, **kw) -> dict:
        self.calls.append(kw.get("amount"))
        return {"ok": True}


class HumanGate:
    """Escalates a high-consequence call to a human (kind='human')."""

    name = "approver"

    def check(self, call, ctx):
        if call.name == "wire_transfer":
            return Verdict(
                severity=Severity.ESCALATE, detector=self.name, detail="needs approval", kind="human"
            )
        return None


def _reg(tool: WireTransfer) -> Registry:
    reg = Registry()
    reg.register("tools", "wire_transfer", tool)
    reg.register("gates", "approver", HumanGate())
    return reg


async def _resolve(bus: EventBus, tid, approval_id: str, idem: str, decision: str = "approve") -> None:
    # The human writes the resolution to the log, bound to the exact invocation.
    await bus.publish(
        Event(
            trace_id=tid,
            task_id=tid,
            type=ev.APPROVAL_RESOLVED,
            source="human",
            payload={
                "approval_id": approval_id,
                "decision": decision,
                "idempotency_key": idem,
                "by": "alice",
            },
        )
    )


async def test_pause_renders_ground_truth_then_resume_executes_once() -> None:
    tool = WireTransfer()
    reg = _reg(tool)
    tid = uuid4()
    bus = EventBus()

    # Phase 1: run to a pause.
    p1 = ScriptedProvider.from_moves([{"tool": "wire_transfer", "args": {"amount": 500}}])
    r1 = await run_task(TaskSpec(task_id=tid, query="pay rent"), p1, reg, bus)
    assert r1.status == Status.PAUSED
    assert tool.calls == []  # NOT executed yet

    events = await bus.query()
    req = [e for e in events if e.type == ev.APPROVAL_REQUESTED][-1]
    # ZU-CD-1: the human sees the harness's literal parameters.
    assert req.payload["args"] == {"amount": 500}
    paused = [e for e in events if e.type == ev.RUN_PAUSED][-1]
    assert paused.payload["pending"]["args"] == {"amount": 500}
    assert paused.payload["tier"] == 1 and paused.payload["tainted"] is False

    # The human approves, bound to the exact idempotency key.
    await _resolve(bus, tid, req.payload["approval_id"], req.payload["idempotency_key"])

    # Phase 2: resume from the log; only the pending invocation executes.
    p2 = ScriptedProvider.from_moves([{"text": '{"done": true}', "finish": "stop"}])
    r2 = await run_task(
        TaskSpec(task_id=tid, query="pay rent"), p2, reg, bus,
        resume_from=await bus.query(),
    )
    assert r2.status == Status.SUCCESS
    assert tool.calls == [500]  # executed EXACTLY once, unchanged

    resumed = await bus.query()
    assert any(e.type == ev.RUN_RESUMED for e in resumed)
    approved = [
        e for e in resumed
        if e.type == ev.GATE_DECIDED and e.payload.get("decision") == "approved_by_human"
    ]
    assert approved  # the resumed call was recorded as human-approved


async def test_resume_with_wrong_key_is_rejected() -> None:
    # ZU-CD-2: an approval whose idempotency key does not bind to the paused
    # invocation must not authorize it (approve-then-swap defeated).
    tool = WireTransfer()
    reg = _reg(tool)
    tid = uuid4()
    bus = EventBus()

    p1 = ScriptedProvider.from_moves([{"tool": "wire_transfer", "args": {"amount": 500}}])
    await run_task(TaskSpec(task_id=tid, query="pay"), p1, reg, bus)
    req = [e for e in await bus.query() if e.type == ev.APPROVAL_REQUESTED][-1]

    # Approve, but bind to a DIFFERENT (forged) idempotency key.
    await _resolve(bus, tid, req.payload["approval_id"], "not-the-right-key")

    p2 = ScriptedProvider.from_moves([{"text": "{}", "finish": "stop"}])
    r2 = await run_task(
        TaskSpec(task_id=tid, query="pay"), p2, reg, bus, resume_from=await bus.query()
    )
    assert r2.status == Status.SUCCESS  # the run continues...
    assert tool.calls == []  # ...but the unapproved invocation never executed
    assert any(
        e.type == ev.DEFENSE_BLOCKED and e.payload.get("kind") == "human_denied"
        for e in await bus.query()
    )


async def test_resume_without_resolution_stays_paused() -> None:
    # Resuming before a human has decided keeps the run paused (idempotent).
    tool = WireTransfer()
    reg = _reg(tool)
    tid = uuid4()
    bus = EventBus()

    p1 = ScriptedProvider.from_moves([{"tool": "wire_transfer", "args": {"amount": 1}}])
    await run_task(TaskSpec(task_id=tid, query="q"), p1, reg, bus)

    p2 = ScriptedProvider.from_moves([{"text": "{}", "finish": "stop"}])
    r2 = await run_task(
        TaskSpec(task_id=tid, query="q"), p2, reg, bus, resume_from=await bus.query()
    )
    assert r2.status == Status.PAUSED
    assert tool.calls == []
