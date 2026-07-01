"""Offline proofs for the seven low-severity zu-core cleanups (#65: C1/C3/C8/
C11/C14/C10/F81).

Every test is $0: a pure unit assertion or a ScriptedProvider (fake model) run
against an event-log oracle — no api key, no network, no Docker. Each is written
to FAIL on the pre-change code and pass on the fix.
"""

from __future__ import annotations

import pytest

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.contracts import Result, Status, TaskSpec
from zu_core.grants import InMemoryGrantStore
from zu_core.ledger import InMemoryExecutionLedger
from zu_core.loop import PluginProtocolError, _EventsView, run_task
from zu_core.ports import (
    MonitorVerdict,
    RunContext,
    Scope,
    ToolCall,
    Verdict,
)
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider


# --------------------------------------------------------------------------- #
# helpers: minimal, generic plugins (no site constants)
# --------------------------------------------------------------------------- #
class _Echo:
    """An inert tier-1 tool that just returns a fixed observation."""

    name = "echo"
    tier = 1
    schema = {
        "name": "echo",
        "parameters": {
            "type": "object",
            "properties": {"n": {"type": "integer"}},
            "required": ["n"],
        },
    }
    prompt_fragment = "echo(n)"
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    async def __call__(self, ctx, **kw) -> dict:
        return {"text": "ok", "got": kw}


def _echo_registry() -> Registry:
    reg = Registry()
    reg.register("tools", "echo", _Echo())
    return reg


# --------------------------------------------------------------------------- #
# C1 — RunContext carries typed (not bare-Any) plugin state where knowable
# --------------------------------------------------------------------------- #
def test_c1_runcontext_narrows_grants_execution_invocation() -> None:
    # The fields the loop populates from real objects are now Protocol/contract
    # typed, not `Any`. A pydantic model records its field annotations, so we can
    # assert the tightening executably (on old code these were all `Any`).
    ann = RunContext.model_fields
    grants_t = ann["grants"].annotation
    exec_t = ann["execution"].annotation
    inv_t = ann["invocation"].annotation
    # invocation is the concrete ToolCall contract (optional).
    assert ToolCall in getattr(inv_t, "__args__", (inv_t,))
    # grants/execution resolved to their port Protocols (not bare Any). `Any` has
    # no __args__ and is the object `typing.Any`; a narrowed Optional[Proto] does.
    from typing import Any as _Any

    assert grants_t is not _Any and exec_t is not _Any
    # And a real store/ledger validates into the typed fields.
    ctx = RunContext(
        spec=object(),
        grants=InMemoryGrantStore(),
        execution=InMemoryExecutionLedger(),
        invocation=ToolCall(name="echo", args={"n": 1}),
    )
    assert ctx.invocation is not None and ctx.invocation.name == "echo"
    # Backward compatible: the open default (None) still validates.
    assert RunContext(spec=object()).grants is None


# --------------------------------------------------------------------------- #
# C3 — materialized plugins are protocol-checked after instantiation
# --------------------------------------------------------------------------- #
async def test_c3_misregistered_plugin_fails_loudly_naming_kind() -> None:
    # A monitor-shaped object (has `evaluate`, lacks `__call__`) registered under
    # the WRONG kind ("tools"). On old code this slipped in and blew up cryptically
    # only when the loop tried to call it; now _materialize refuses it at load,
    # naming the plugin + kind.
    class NotATool:  # a Monitor shape, not a Tool
        name = "imposter"

        def evaluate(self, ctx):  # noqa: ANN001
            return None

    reg = Registry()
    reg.register("tools", "imposter", NotATool())
    provider = ScriptedProvider.from_moves([{"text": "{}", "finish": "stop"}])
    with pytest.raises(PluginProtocolError) as ei:
        await run_task(TaskSpec(query="q"), provider, reg, EventBus())
    assert "imposter" in str(ei.value)
    assert "tool" in str(ei.value).lower()


async def test_c3_valid_minimal_tool_is_admitted() -> None:
    # A lean-but-valid tool (name + __call__, everything else read defensively)
    # must NOT be rejected by the protocol check.
    provider = ScriptedProvider.from_moves(
        [{"tool": "echo", "args": {"n": 1}}, {"text": '{"ok": true}', "finish": "stop"}]
    )
    result = await run_task(TaskSpec(query="q"), provider, _echo_registry(), EventBus())
    assert result.status == Status.SUCCESS


# --------------------------------------------------------------------------- #
# C8 — model tool-call args validated against the declared schema before dispatch
# --------------------------------------------------------------------------- #
async def test_c8_bad_args_are_rejected_before_dispatch() -> None:
    # echo declares n:integer required. The model supplies a string, and an extra
    # unknown key. On old code these bad args were dispatched straight to the tool;
    # now the loop emits a schema-rejection and the tool body never runs.
    called: list = []

    class Guarded(_Echo):
        async def __call__(self, ctx, **kw) -> dict:
            called.append(kw)
            return {"text": "ran"}

    reg = Registry()
    reg.register("tools", "echo", Guarded())
    provider = ScriptedProvider.from_moves(
        [{"tool": "echo", "args": {"n": "not-an-int"}}, {"text": '{"ok": true}', "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert result.status == Status.SUCCESS  # a bad call is contained, run continues
    events = await bus.query()
    blocked = [e for e in events if e.type == ev.DEFENSE_BLOCKED]
    assert any(e.payload.get("kind") == "schema_mismatch" for e in blocked)
    assert called == []  # the tool body never ran on invalid args


async def test_c8_valid_args_still_dispatch() -> None:
    # Well-formed args must pass straight through — the validation is generic and
    # never rejects a conforming call.
    provider = ScriptedProvider.from_moves(
        [{"tool": "echo", "args": {"n": 3}}, {"text": '{"ok": true}', "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, _echo_registry(), bus)
    assert result.status == Status.SUCCESS
    returned = [e for e in await bus.query() if e.type == ev.TOOL_RETURNED]
    # echo returns a `text` content field (summarized in tool.returned) plus the
    # kwargs it received — the conforming call ran and got n=3.
    assert any(e.payload.get("observation", {}).get("got") == {"n": 3} for e in returned)
    # no schema rejection for a conforming call
    assert not [
        e for e in await bus.query()
        if e.type == ev.DEFENSE_BLOCKED and e.payload.get("kind") == "schema_mismatch"
    ]


# --------------------------------------------------------------------------- #
# C11 — _EventsView read-only contract is ENFORCED, not convention
# --------------------------------------------------------------------------- #
def test_c11_events_view_has_no_public_mutable_backing() -> None:
    backing = [1, 2, 3]
    view = _EventsView(backing)
    # No public `_events` handle to reach and mutate.
    assert not hasattr(view, "_events")
    # The Sequence surface exposes no mutators.
    for mutator in ("append", "__setitem__", "__delitem__", "insert", "extend"):
        assert not hasattr(view, mutator)
    # It still reflects the live list by reference (read-only window).
    assert list(view) == [1, 2, 3]
    backing.append(4)
    assert len(view) == 4 and view[-1] == 4
    # Attempting to mutate through the view raises, never silently corrupts.
    with pytest.raises(TypeError):
        view[0] = 99  # type: ignore[index]
    assert backing[0] == 1  # canonical list untouched


# --------------------------------------------------------------------------- #
# C14 — a detector's declared Scope is enforced against events it can read
# --------------------------------------------------------------------------- #
async def test_c14_per_observation_detector_is_scoped_to_current_turn() -> None:
    # A PER_OBSERVATION detector records how many events its ctx.events window
    # exposes. Across a two-tool-call run its window must NOT keep growing with the
    # whole log — it is pinned to the current turn. On old code it saw the entire
    # (ever-growing) log; now it sees only the current turn's suffix.
    class WindowSpy:
        name = "window_spy"
        scope = Scope.PER_OBSERVATION

        def __init__(self) -> None:
            self.first_event_types: list = []

        def inspect(self, ctx: RunContext) -> None:
            # The earliest event visible in this scoped window: under the fix it is
            # the current turn's TURN_STARTED, never harness.task.started (which is
            # outside the observation's scope).
            if len(ctx.events) > 0:
                self.first_event_types.append(ctx.events[0].type)
            return None

    spy = WindowSpy()
    reg = _echo_registry()
    reg.register("detectors", "window_spy", spy)
    provider = ScriptedProvider.from_moves(
        [
            {"tool": "echo", "args": {"n": 1}},
            {"tool": "echo", "args": {"n": 2}},
            {"text": '{"ok": true}', "finish": "stop"},
        ]
    )
    await run_task(TaskSpec(query="q"), provider, reg, EventBus())
    assert spy.first_event_types, "detector never ran"
    # The scoped window never reaches back to the run's first event.
    assert ev.TASK_STARTED not in spy.first_event_types
    # Every window it saw begins at a turn boundary (its own scope), proving it
    # cannot read events outside the current observation.
    assert all(t == ev.TURN_STARTED for t in spy.first_event_types)


# --------------------------------------------------------------------------- #
# C10 — detector/monitor/validator/replay-arbiter crashes are surfaced + counted
# --------------------------------------------------------------------------- #
async def test_c10_crashing_detector_emits_and_counts_a_crash_event() -> None:
    class Boom:
        name = "boom_detector"
        scope = Scope.PER_OBSERVATION

        def inspect(self, ctx: RunContext) -> None:
            raise RuntimeError("kaboom")

    reg = _echo_registry()
    reg.register("detectors", "boom_detector", Boom())
    provider = ScriptedProvider.from_moves(
        [{"tool": "echo", "args": {"n": 1}}, {"text": '{"ok": true}', "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert result.status == Status.SUCCESS  # a broken detector never halts the run
    crashed = [e for e in await bus.query() if e.type == ev.CHECK_CRASHED]
    assert crashed, "a crashing detector must surface a harness.check.crashed event"
    assert crashed[0].payload["kind"] == "detector"
    assert crashed[0].payload["name"] == "boom_detector"
    assert "kaboom" in crashed[0].payload["error"]


async def test_c10_crashing_monitor_and_validator_are_surfaced() -> None:
    class BoomMonitor:
        name = "boom_monitor"

        def evaluate(self, ctx: RunContext) -> MonitorVerdict:
            raise RuntimeError("monitor-down")

    class BoomValidator:
        name = "boom_validator"

        def check(self, result: Result, ctx: RunContext) -> Verdict:
            raise RuntimeError("validator-down")

    reg = _echo_registry()
    reg.register("monitors", "boom_monitor", BoomMonitor())
    reg.register("validators", "boom_validator", BoomValidator())
    provider = ScriptedProvider.from_moves(
        [{"tool": "echo", "args": {"n": 1}}, {"text": '{"ok": true}', "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, reg, bus)
    assert result.status == Status.SUCCESS
    crashed = [e for e in await bus.query() if e.type == ev.CHECK_CRASHED]
    kinds = {e.payload["kind"] for e in crashed}
    assert "monitor" in kinds
    assert "validator" in kinds


# --------------------------------------------------------------------------- #
# F81 — the containment='required' basis is recorded onto the audit log
# --------------------------------------------------------------------------- #
async def test_f81_forged_sandbox_signal_is_recorded_as_uncorroborated(monkeypatch) -> None:
    # A bare, FORGED ZU_SANDBOXED=1 (no launcher proxy/network wiring) passes the
    # fail-closed floor (it always did — the env is forgeable), but the basis is now
    # on the audit log with corroborated=False, so the forgery is DETECTABLE after
    # the fact. On old code no such record existed at all.
    monkeypatch.setenv("ZU_SANDBOXED", "1")
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("ZU_SANDBOX_NETWORK", raising=False)
    provider = ScriptedProvider.from_moves([{"text": "{}", "finish": "stop"}])
    bus = EventBus()
    await run_task(
        TaskSpec(query="q"), provider, _echo_registry(), bus, containment="required"
    )
    attested = [e for e in await bus.query() if e.type == ev.CONTAINMENT_ATTESTED]
    assert len(attested) == 1
    basis = attested[0].payload
    assert basis["sandboxed"] is True
    assert basis["corroborated"] is False  # forged: proxy/network wiring absent


async def test_f81_corroborated_when_launcher_signals_present(monkeypatch) -> None:
    # With the launcher's structural signals present alongside ZU_SANDBOXED, the
    # basis is corroborated — a real contained run is distinguishable from a forgery.
    monkeypatch.setenv("ZU_SANDBOXED", "1")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    monkeypatch.setenv("ZU_SANDBOX_NETWORK", "zu-internal")
    provider = ScriptedProvider.from_moves([{"text": "{}", "finish": "stop"}])
    bus = EventBus()
    await run_task(
        TaskSpec(query="q"), provider, _echo_registry(), bus, containment="required"
    )
    attested = [e for e in await bus.query() if e.type == ev.CONTAINMENT_ATTESTED]
    assert attested and attested[0].payload["corroborated"] is True


async def test_f81_no_attestation_under_audit_default() -> None:
    # The event is only emitted under the load-bearing 'required' posture, so every
    # other run's event sequence is unchanged.
    provider = ScriptedProvider.from_moves([{"text": "{}", "finish": "stop"}])
    bus = EventBus()
    await run_task(TaskSpec(query="q"), provider, _echo_registry(), bus)  # audit default
    assert not [e for e in await bus.query() if e.type == ev.CONTAINMENT_ATTESTED]
