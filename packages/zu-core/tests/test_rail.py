"""ZU-RAIL-1/2/3/4 — the rail mechanisms a delegated-action consumer needs from Zu.

Proved offline with the ScriptedProvider (fake model) and replayed tracks:
  * RAIL-1 — a rail replays only when it matches the human-approved content hash;
  * RAIL-2 — explore mode mechanically disarms capability-bearing calls;
  * RAIL-3 — a ReplayArbiter escalates a consequential divergence to a HUMAN;
  * RAIL-4 — consequence/destination annotations round-trip and reach the gate.

Mechanism is upstream (here); the diff metric / classifier / thresholds are the
consumer's policy and never appear in these tests as Zu code.
"""

from __future__ import annotations

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.contracts import Status, TaskSpec
from zu_core.loop import run_task
from zu_core.ports import ReplayDecision
from zu_core.registry import Registry
from zu_core.track import Track, TrackStep, record_track
from zu_providers.scripted import ScriptedProvider


class Rec:
    """An inert tier-1 recording tool (no capability/egress)."""

    name = "rec"
    tier = 1
    schema = {"name": "rec", "parameters": {"type": "object", "properties": {}}}
    prompt_fragment = "rec()"
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    def __init__(self) -> None:
        self.calls: list = []

    async def __call__(self, ctx, **kw) -> dict:
        self.calls.append(kw)
        return {"text": f"did {kw}"}


class NetTool:
    """A capability-bearing tool (declares egress) — disarmed in explore mode."""

    name = "net_call"
    tier = 1
    schema = {"name": "net_call", "parameters": {"type": "object", "properties": {}}}
    prompt_fragment = "net_call()"
    capabilities: frozenset[str] = frozenset({"net"})
    egress: frozenset[str] = frozenset({"*"})

    def __init__(self) -> None:
        self.calls: list = []

    async def __call__(self, ctx) -> dict:
        self.calls.append(1)
        return {"ok": True}


# --- ZU-RAIL-1: a rail replays only when it matches the approved content hash ---


def test_content_hash_is_stable_over_pacing_but_not_args() -> None:
    a = Track(task="q", steps=[TrackStep("rec", {"k": "a"}, wait_ms=10)])
    b = Track(task="q", steps=[TrackStep("rec", {"k": "a"}, wait_ms=9999)])  # only pacing differs
    c = Track(task="q", steps=[TrackStep("rec", {"k": "EVIL"}, wait_ms=10)])  # args differ
    assert a.content_hash() == b.content_hash()  # wait_ms excluded (cosmetic)
    assert a.content_hash() != c.content_hash()  # semantic change moves the hash


async def test_approved_rail_replays_and_is_verified() -> None:
    tool = Rec()
    reg = Registry()
    reg.register("tools", "rec", tool)
    track = Track(task="q", steps=[TrackStep("rec", {"k": "a"}, 0), TrackStep("rec", {"k": "b"}, 0)])
    provider = ScriptedProvider.from_moves([{"text": '{"ok": true}', "finish": "stop"}])
    bus = EventBus()
    result = await run_task(
        TaskSpec(query="q"), provider, reg, bus, track=track, approved_rail_hash=track.content_hash()
    )
    assert result.status == Status.SUCCESS
    assert [c["k"] for c in tool.calls] == ["a", "b"]  # the approved rail ran
    assert any(e.type == ev.RAIL_VERIFIED for e in await bus.query())


async def test_unapproved_rail_is_refused_before_any_step() -> None:
    approved = Track(task="q", steps=[TrackStep("rec", {"k": "a"}, 0)]).content_hash()
    tool = Rec()
    reg = Registry()
    reg.register("tools", "rec", tool)
    # The rail being replayed differs from what was approved (a tampered/substituted
    # step) — its content hash no longer matches.
    tampered = Track(task="q", steps=[TrackStep("rec", {"k": "EVIL"}, 0)])
    provider = ScriptedProvider.from_moves([{"text": '{"ok": true}', "finish": "stop"}])
    bus = EventBus()
    result = await run_task(
        TaskSpec(query="q"), provider, reg, bus, track=tampered, approved_rail_hash=approved
    )
    assert result.status == Status.TERMINAL and result.reason == "rail.unapproved"
    assert tool.calls == []  # refused before any step ran
    assert any(
        e.type == ev.DEFENSE_BLOCKED and e.payload.get("kind") == "rail_unapproved"
        for e in await bus.query()
    )


# --- ZU-RAIL-2: explore mode disarms capability-bearing calls -----------------


async def test_explore_mode_disarms_capability_bearing_call() -> None:
    tool = NetTool()
    reg = Registry()
    reg.register("tools", "net_call", tool)
    provider = ScriptedProvider.from_moves(
        [{"tool": "net_call", "args": {}}, {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q", mode="explore"), provider, reg, bus)
    assert result.status == Status.SUCCESS
    assert tool.calls == []  # NOT executed — disarmed during exploration
    events = await bus.query()
    assert any(e.type == ev.RAIL_DISARMED and e.payload.get("tool") == "net_call" for e in events)
    started = [e for e in events if e.type == ev.TASK_STARTED][0]
    assert started.payload["mode"] == "explore"


async def test_execute_mode_runs_the_capability_bearing_call() -> None:
    tool = NetTool()
    reg = Registry()
    reg.register("tools", "net_call", tool)
    provider = ScriptedProvider.from_moves(
        [{"tool": "net_call", "args": {}}, {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    # default mode == "execute"
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert result.status == Status.SUCCESS and tool.calls == [1]  # armed, ran


async def test_explore_mode_still_runs_inert_tier1_tools() -> None:
    tool = Rec()  # no capability/egress
    reg = Registry()
    reg.register("tools", "rec", tool)
    provider = ScriptedProvider.from_moves(
        [{"tool": "rec", "args": {"k": "x"}}, {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    await run_task(TaskSpec(query="q", mode="explore"), provider, reg, bus)
    assert tool.calls == [{"k": "x"}]  # inert tool runs even in explore


# --- ZU-RAIL-3: a ReplayArbiter escalates consequential divergence to a HUMAN --


class _ConsequenceArbiter:
    """A scripted arbiter: it returns a fixed decision for HIGH steps. Stands in for
    a consumer's edge router; the diff metric/thresholds are NOT here (that's policy)."""

    name = "consequence_arbiter"

    def __init__(self, on_high: ReplayDecision) -> None:
        self._on_high = on_high

    def decide(self, step, observation, ctx) -> ReplayDecision:
        if getattr(step, "consequence", None) == "HIGH":
            return self._on_high
        return ReplayDecision.CONTINUE


def _reg_with_arbiter(tool: Rec, decision: ReplayDecision) -> Registry:
    reg = Registry()
    reg.register("tools", "rec", tool)
    reg.register("replay_arbiters", "consequence_arbiter", _ConsequenceArbiter(decision))
    return reg


async def test_arbiter_escalates_high_step_to_human() -> None:
    tool = Rec()
    reg = _reg_with_arbiter(tool, ReplayDecision.ESCALATE)
    track = Track(task="q", steps=[TrackStep("rec", {"amount": 500}, 0, consequence="HIGH")])
    provider = ScriptedProvider.from_moves([{"text": "{}", "finish": "stop"}])
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus, track=track)

    assert result.status == Status.PAUSED  # escalated to a human, not the model
    assert tool.calls == []  # the consequential step did NOT run
    req = [e for e in await bus.query() if e.type == ev.APPROVAL_REQUESTED][-1]
    assert req.payload["args"] == {"amount": 500}  # the literal step, harness ground truth


async def test_arbiter_stop_ends_the_run() -> None:
    tool = Rec()
    reg = _reg_with_arbiter(tool, ReplayDecision.STOP)
    track = Track(task="q", steps=[TrackStep("rec", {"amount": 500}, 0, consequence="HIGH")])
    provider = ScriptedProvider.from_moves([{"text": "{}", "finish": "stop"}])
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus, track=track)
    assert result.status == Status.TERMINAL and result.reason == "replay.arbiter.stop"
    assert tool.calls == []


async def test_arbiter_continue_keeps_replaying() -> None:
    tool = Rec()
    reg = _reg_with_arbiter(tool, ReplayDecision.ESCALATE)  # only HIGH escalates
    # all LOW steps -> arbiter returns CONTINUE -> replay proceeds normally
    track = Track(task="q", steps=[TrackStep("rec", {"k": "a"}, 0, consequence="LOW"),
                                   TrackStep("rec", {"k": "b"}, 0, consequence="LOW")])
    provider = ScriptedProvider.from_moves([{"text": '{"ok": true}', "finish": "stop"}])
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus, track=track)
    assert result.status == Status.SUCCESS and [c["k"] for c in tool.calls] == ["a", "b"]


async def test_no_arbiter_replays_unchanged() -> None:
    # Regression guard: with NO arbiter registered, replay behaves exactly as before.
    tool = Rec()
    reg = Registry()
    reg.register("tools", "rec", tool)
    track = Track(task="q", steps=[TrackStep("rec", {"k": "a"}, 0), TrackStep("rec", {"k": "b"}, 0)])
    provider = ScriptedProvider.from_moves([{"text": '{"ok": true}', "finish": "stop"}])
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus, track=track)
    assert result.status == Status.SUCCESS and [c["k"] for c in tool.calls] == ["a", "b"]


# --- ZU-RAIL-4: consequence/destination annotations round-trip and reach the gate


def test_annotations_roundtrip_record_and_serialize() -> None:
    class _Ev:
        def __init__(self, type, payload, ts=None):
            self.type = type
            self.payload = payload
            self.ts = ts

    events = [
        _Ev("harness.tool.invoked",
            {"tool": "pay", "args": {"amount": 5},
             "ctx": {"consequence": "HIGH", "destination": "merchant:acme"}}),
    ]
    track = record_track(events, task="q", model=None)
    assert track.steps[0].consequence == "HIGH"
    assert track.steps[0].destination == "merchant:acme"
    # round-trips through JSON
    back = Track.from_json(track.to_json())
    assert back.steps[0].consequence == "HIGH" and back.steps[0].destination == "merchant:acme"


async def test_annotations_reach_the_replayed_tool_invoked_ctx() -> None:
    tool = Rec()
    reg = Registry()
    reg.register("tools", "rec", tool)
    track = Track(task="q", steps=[TrackStep("rec", {"k": "a"}, 0,
                                             consequence="LOW", destination="origin:example.com")])
    provider = ScriptedProvider.from_moves([{"text": "{}", "finish": "stop"}])
    bus = EventBus()
    await run_task(TaskSpec(query="q"), provider, reg, bus, track=track)
    invoked = [e for e in await bus.query() if e.type == ev.TOOL_INVOKED][0]
    assert invoked.payload["ctx"]["consequence"] == "LOW"
    assert invoked.payload["ctx"]["destination"] == "origin:example.com"
