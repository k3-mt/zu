"""Resumability — two MUTUALLY EXCLUSIVE strategies, offline ($0).

A run that escalates stops at ``escalated_at`` (the resume cursor). On each
SUCCESSFUL step ``on_checkpoint(i)`` fires, so ``escalated_at`` maps 1:1 to a
last-known-good cursor.

* Strategy 1 — REPLAY: re-walk the SAME recorded path up to the stuck point by
  re-running ``execute(steps[:escalated_at])`` on a fresh session. It lands at the
  same step, and because the diagnostic read journals ``CONTENT_CAPTURED``
  (provenance + hash, never body), the resumed run asserts it re-perceived the
  SAME content by hash match (shadow-replay correctness) — at $0, no parser.

* Strategy 2 — NAVIGATE/REPLAN: pick a DIFFERENT path. A prior event log with a
  ``CHECKPOINT_MARKED`` → ``last_known_good`` returns it, and
  ``rollback_and_replan`` re-seats at the good prefix (failed tail dropped) for a
  different route.

The mutual exclusion is load-bearing: ``rollback_and_replan`` deliberately does
NOT thread replay kwargs (Issue #41 §6). The two are never combined in one
re-seat.
"""

from __future__ import annotations

from uuid import uuid4

from zu_core import events as ev
from zu_core.bus import EventBus
from zu_core.content_view import (
    ContentView,
    FieldState,
    Provenance,
    Want,
)
from zu_core.contracts import Event, Status, TaskSpec
from zu_core.escalation import ProblemContext, Repair
from zu_core.loop import _rebuild_run_state, _rebuild_to, last_known_good, rollback_and_replan
from zu_core.ports import ModelProvider
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider
from zu_shadow.executor import Step, execute


class _HumanRepairer:
    """A repairer that always routes to a human — it reads the diagnostic slice
    (so CONTENT_CAPTURED + STEP_ESCALATED are journalled) then stops the run."""

    async def diagnose_and_repair(
        self, ctx: ProblemContext, model: ModelProvider, *, budget: int
    ) -> Repair:
        return Repair("human", reason="route to a person")

_TRACE = uuid4()
_TASK = uuid4()


def _diag(label: str) -> ContentView:
    prov = Provenance(url="https://shop.example/checkout", region="form#checkout")
    return ContentView(
        url="https://shop.example/checkout",
        field_states=(FieldState(label=label, value=None, required=True, invalid=True,
                                 error_text="Required", provenance=prov),),
    )


class StuckSession:
    """A session that gets stuck on the LAST of ``n_ok`` resolvable steps: the
    first ``n_ok`` steps resolve and act; thereafter ``perceive`` returns an empty
    surface (no resolvable handle) → the executor escalates. ``content_view``
    returns a fixed diagnostic slice so a resumed run re-reads the SAME content."""

    def __init__(self, n_ok: int, diagnostic: ContentView) -> None:
        self._n_ok = n_ok
        self._diag = diagnostic
        self._step = 0
        self.content_reads: list[frozenset[Want]] = []

    def perceive(self):  # noqa: ANN201 - SurfaceView, kept terse for the test double
        from zu_core.surface import SurfaceAffordance, SurfaceView
        if self._step < self._n_ok:
            # Each successful step lands on a DISTINCT page (the url carries the step
            # index) so a genuine transition is never read as a no-op.
            return SurfaceView(url=f"https://shop.example/{self._step}", title="Shop",
                               affordances=(SurfaceAffordance(handle="a1", role="button",
                                                              label="Go"),))
        return SurfaceView()  # nothing resolvable → escalate

    def act(self, handle: str, kind: str, value: str | None = None) -> None:
        self._step += 1

    def current_url(self) -> str:
        return "https://shop.example"

    def content_view(self, want: frozenset[Want]) -> ContentView:
        self.content_reads.append(want)
        return self._diag


def _ev(type_: str, payload: dict, *, event_id=None) -> Event:
    kw = {"event_id": event_id} if event_id is not None else {}
    return Event(trace_id=_TRACE, task_id=_TASK, type=type_, source="test",
                 payload=payload, **kw)


# ---------- Strategy 1: replay to the stuck point ----------------------------


async def test_resume_by_replay_lands_at_the_same_step_with_matching_hash() -> None:
    # Three "Go" clicks; the third is unreachable → escalate at index 2.
    steps = [Step(kind="click", role="button", name="Go") for _ in range(3)]
    diag = _diag("Last name")

    bus1 = EventBus()
    checkpoints: list[int] = []

    async def _cp(i: int) -> None:
        checkpoints.append(i)

    session1 = StuckSession(n_ok=2, diagnostic=diag)
    report1 = await execute(steps, session1, ScriptedProvider.from_moves([]),
                            repairer=_HumanRepairer(), bus=bus1, on_checkpoint=_cp,
                            trace_id=_TRACE, task_id=_TASK)
    assert report1.escalated_at == 2
    # on_checkpoint fired on the two SUCCESSFUL steps before the stuck one.
    assert checkpoints == [0, 1]
    cap1 = next(e for e in await bus1.query() if e.type == ev.CONTENT_CAPTURED)
    await bus1.aclose()

    # REPLAY: re-run the recorded prefix up to the stuck point on a FRESH session.
    bus2 = EventBus()
    session2 = StuckSession(n_ok=2, diagnostic=diag)
    report2 = await execute(steps[: report1.escalated_at], session2,
                            ScriptedProvider.from_moves([]), bus=bus2,
                            trace_id=_TRACE, task_id=_TASK)
    # The prefix completes — it lands exactly at the step we got stuck on.
    assert report2.completed
    assert len(report2.acted) == 2
    # No content read on the clean prefix replay (no escalation in steps[:2]).
    assert session2.content_reads == []
    await bus2.aclose()

    # Shadow-replay correctness: re-perceiving the SAME diagnostic yields the SAME
    # view hash (the journalled signal), so a resumed run can assert it re-perceived
    # the same content — at $0, no parser.
    assert cap1.payload["view_hash"] == diag.hash()


# ---------- Strategy 2: navigate / rollback-and-replan -----------------------


def test_last_known_good_returns_the_marker() -> None:
    start = _ev(ev.TASK_STARTED, {"query": "q", "tainted": False})
    marker = _ev(ev.CHECKPOINT_MARKED, {"label": "step:1", "step": 1})
    log = [start, _ev(ev.TOOL_INVOKED, {"tool": "x"}), marker]
    assert last_known_good(log) == marker.event_id


def test_rollback_re_seats_the_good_prefix() -> None:
    # A prior log that marks a good point then fails: the good-prefix fold differs
    # from the full fold (the failed tail is dropped) — proving the re-seat.
    start = _ev(ev.TASK_STARTED, {"query": "q", "tainted": False})
    marker = _ev(ev.CHECKPOINT_MARKED, {"label": "step:1", "step": 1})
    climb = _ev(ev.TASK_ESCALATED, {"from_tier": 1, "to_tier": 2})
    log = [start, marker, climb, _ev(ev.TASK_TERMINAL, {"reason": "boom"})]
    full = _rebuild_run_state(log)
    prefix = _rebuild_to(log, marker.event_id)
    assert full["tier"] == 2 and prefix["tier"] == 1  # the failed tail was dropped


class _Echo:
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


async def test_rollback_and_replan_picks_a_different_path() -> None:
    # Strategy 2 end-to-end: a marked good prefix + a failed tail → rollback re-seats
    # and the model re-plans a DIFFERENT path. This is the event-sourced arm, distinct
    # from the shadow replay above.
    from zu_core.loop import run_task

    reg = Registry()
    reg.register("tools", "echo", _Echo())
    prior_bus = EventBus()
    await run_task(TaskSpec(query="q"),
                   ScriptedProvider.from_moves([{"tool": "echo", "args": {"good": 1}},
                                                {"text": '{"ok": true}', "finish": "stop"}]),
                   reg, prior_bus, trace_id=_TRACE)
    prior = list(await prior_bus.query())
    prior.append(_ev(ev.CHECKPOINT_MARKED, {"label": "step:0", "step": len(prior)}))
    prior.append(_ev(ev.TOOL_INVOKED, {"tool": "echo"}))
    prior.append(_ev(ev.TASK_TERMINAL, {"reason": "off_rail"}))
    await prior_bus.aclose()

    target = _Echo()
    reg2 = Registry()
    reg2.register("tools", "echo", target)
    bus = EventBus()
    result = await rollback_and_replan(
        TaskSpec(query="q"),
        ScriptedProvider.from_moves([{"tool": "echo", "args": {"different": 99}},
                                     {"text": '{"ok": true}', "finish": "stop"}]),
        prior=prior, registry=reg2, bus=bus, trace_id=_TRACE,
    )
    assert result.status == Status.SUCCESS
    assert target.calls == [{"different": 99}]  # the DIFFERENT path was taken
    await bus.aclose()


def test_the_two_strategies_are_not_combined() -> None:
    # The mutual exclusion is a TYPE-LEVEL fact: rollback_and_replan exposes no
    # replay/track kwarg, so a recorded shadow track can never be threaded through a
    # rollback re-seat (that would re-walk the FAILED route). Replay (Strategy 1)
    # lives entirely in zu_shadow.execute; rollback (Strategy 2) in zu_core.loop.
    import inspect

    params = set(inspect.signature(rollback_and_replan).parameters)
    assert not (params & {"track", "replay_budget", "steps", "finish_provider"})
    # And execute (the replay arm) does not accept a prior-event-log rollback kwarg.
    exec_params = set(inspect.signature(execute).parameters)
    assert not (exec_params & {"prior", "rollback", "lkg"})
