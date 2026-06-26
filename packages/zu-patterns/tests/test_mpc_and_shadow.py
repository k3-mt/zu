"""The LIVE guided-MPC loop (§5.2) + the Shadow-sourced transition model (Part B).

All $0 and deterministic: a ScriptedProvider proposes K candidates, a hand-built
``reachability.Fsm`` is the learned model, and a fake executor returns scripted
next-surfaces. No browser, no network, no keys.

The two load-bearing properties:
  * MODEL PROPOSES, deterministic lookahead+rail DISPOSES — MPC picks the
    GOAL-REACHABLE / on-rail candidate, NOT just the model's first pick.
  * the live loop STOPS at the COMMIT BOUNDARY (default-to-committing) — a
    committing candidate escalates rather than executes.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from zu_core import events as ev
from zu_core.ports import Finish, ModelResponse, ToolCall
from zu_core.reachability import Fsm, FsmEdge
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_patterns.search import (
    Candidate,
    fsm_from_shadow,
    fsm_from_shadow_events,
    live_mpc_step,
    merge_transition_models,
    mpc_run,
)
from zu_providers.scripted import ScriptedProvider


def _ev(etype: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type=etype, payload=payload)


# --- a tiny learned FSM and surfaces, with explicit state ids -------------
#
# here --reach--> good --done--> goal   (the on-rail route)
# here --wander--> dead                 (a trap)
# We map a SurfaceView to its state id explicitly so the test is independent of
# the surface-digest heuristic.


def _branch_fsm() -> Fsm:
    return Fsm(
        states=frozenset({"here", "good", "goal", "dead"}),
        initial="here",
        accepting=frozenset({"goal"}),
        edges=(
            FsmEdge("here", "good", "reach"),
            FsmEdge("good", "goal", "done"),
            FsmEdge("here", "dead", "wander"),
        ),
    )


def _surface(state: str) -> SurfaceView:
    # the affordances are reversible (textbox/link) so the commit boundary does
    # not interfere unless a test makes it a committing op.
    return SurfaceView(
        title=state,
        url=f"https://x/{state}",
        affordances=(
            SurfaceAffordance(handle="a1", role="link", label="reach"),
            SurfaceAffordance(handle="a2", role="link", label="wander"),
        ),
    )


def _state_of(surface: SurfaceView) -> str:
    # the test's explicit surface→state map (last path segment).
    return surface.url.rsplit("/", 1)[-1]


# --- PART A: live_mpc_step disposes via lookahead+rail, not raw model ------


@pytest.mark.asyncio
async def test_mpc_picks_on_rail_over_models_first_pick() -> None:
    # The model proposes the TRAP move ("wander") FIRST, then the on-rail move
    # ("reach"). The deterministic lookahead+rail must DISPOSE for "reach" — the
    # goal-reachable candidate — NOT the model's naive first pick.
    model = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(name="click", args={"label": "wander", "handle": "a2"}),
                    ToolCall(name="click", args={"label": "reach", "handle": "a1"}),
                ],
                finish=Finish.TOOL_CALLS,
            )
        ]
    )
    decision = await live_mpc_step(
        _surface("here"), model, _branch_fsm(), surface_to_state=_state_of
    )
    assert decision.escalate is False
    assert decision.action is not None
    assert decision.action.label == "reach"  # NOT "wander" (the model's first pick)
    # the trap was scored worst.
    by_label = {c.label: s for c, s in decision.scored}
    assert by_label["reach"] > by_label["wander"]


@pytest.mark.asyncio
async def test_mpc_no_on_rail_candidate_escalates() -> None:
    # The model proposes ONLY the trap move. No on-rail candidate ⇒ escalate.
    model = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(name="click", args={"label": "wander", "handle": "a2"})],
                finish=Finish.TOOL_CALLS,
            )
        ]
    )
    decision = await live_mpc_step(
        _surface("here"), model, _branch_fsm(), surface_to_state=_state_of
    )
    assert decision.escalate is True
    assert decision.action is None


@pytest.mark.asyncio
async def test_mpc_stops_at_commit_boundary() -> None:
    # The on-rail candidate is a COMMITTING op ("submit"). The live loop must STOP
    # at the commit boundary (default-to-committing) — escalate, NOT execute.
    model = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[
                    ToolCall(name="submit", args={"label": "reach", "op": "submit"})
                ],
                finish=Finish.TOOL_CALLS,
            )
        ]
    )
    decision = await live_mpc_step(
        _surface("here"), model, _branch_fsm(), surface_to_state=_state_of
    )
    assert decision.escalate is True
    assert decision.committing is True
    assert decision.action is not None and decision.action.label == "reach"


@pytest.mark.asyncio
async def test_mpc_no_proposals_escalates() -> None:
    model = ScriptedProvider([ModelResponse(text="nothing", finish=Finish.STOP)])
    decision = await live_mpc_step(
        _surface("here"), model, _branch_fsm(), surface_to_state=_state_of
    )
    assert decision.escalate is True
    assert decision.action is None


# --- the driver loop: execute-one-via-injected-executor → re-plan ----------


@pytest.mark.asyncio
async def test_mpc_run_drives_to_goal_via_fake_executor() -> None:
    # Two steps: here --reach--> good --done--> goal. The model proposes the right
    # move at each surface; the FAKE executor returns the scripted next-surface.
    moves = [
        ModelResponse(
            tool_calls=[ToolCall(name="click", args={"label": "reach", "handle": "a1"})],
            finish=Finish.TOOL_CALLS,
        ),
        ModelResponse(
            tool_calls=[ToolCall(name="click", args={"label": "done", "handle": "a1"})],
            finish=Finish.TOOL_CALLS,
        ),
    ]
    model = ScriptedProvider(moves)

    nexts = {"reach": _surface("good"), "done": _surface("goal")}

    async def executor(cand: Candidate, surface: SurfaceView) -> SurfaceView:
        return nexts[cand.label]

    # "good" surface must offer a "done" affordance for the model's second move;
    # but the executor returns surfaces by label so we override "good".
    good = SurfaceView(
        title="good", url="https://x/good",
        affordances=(SurfaceAffordance(handle="a1", role="link", label="done"),),
    )
    nexts["reach"] = good

    outcome = await mpc_run(
        _surface("here"), model, _branch_fsm(), executor, surface_to_state=_state_of
    )
    assert outcome.reached_goal is True
    assert outcome.escalated is False
    assert [c.label for c in outcome.steps] == ["reach", "done"]


@pytest.mark.asyncio
async def test_mpc_run_escalates_on_committing_step() -> None:
    # The loop STOPS (escalates) the instant the chosen step is committing, BEFORE
    # the executor is ever called — the commit boundary is never auto-crossed.
    model = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[ToolCall(name="submit", args={"label": "reach", "op": "pay"})],
                finish=Finish.TOOL_CALLS,
            )
        ]
    )
    executed: list[str] = []

    async def executor(cand: Candidate, surface: SurfaceView) -> SurfaceView:
        executed.append(cand.label)
        return _surface("goal")

    outcome = await mpc_run(
        _surface("here"), model, _branch_fsm(), executor, surface_to_state=_state_of
    )
    assert outcome.escalated is True
    assert outcome.reached_goal is False
    assert executed == []  # the committing step never executed


# --- #34: structural rollback + replan a DIFFERENT on-rail sibling on a trap


def _tc(label: str, handle: str = "a1", **extra: object) -> ToolCall:
    return ToolCall(name="click", args={"label": label, "handle": handle, **extra},)


@pytest.mark.asyncio
async def test_mpc_run_rolls_back_and_replans_sibling_on_trap() -> None:
    # here --reach--> good --done--> goal  (on-rail) ; here --wander--> dead (trap).
    # The model proposes the TRAP ('wander') on the first step; with replan_budget>=1
    # the loop rolls back to 'here', EXCLUDES 'wander', and (second model turn) picks
    # the DIFFERENT on-rail sibling 'reach' — driving to goal rather than escalating.
    model = ScriptedProvider(
        [
            ModelResponse(tool_calls=[_tc("wander", "a2")], finish=Finish.TOOL_CALLS),
            ModelResponse(tool_calls=[_tc("reach", "a1")], finish=Finish.TOOL_CALLS),
            ModelResponse(tool_calls=[_tc("done", "a1")], finish=Finish.TOOL_CALLS),
        ]
    )
    good = SurfaceView(
        title="good", url="https://x/good",
        affordances=(SurfaceAffordance(handle="a1", role="link", label="done"),),
    )
    nexts = {"reach": good, "done": _surface("goal")}

    async def executor(cand: Candidate, surface: SurfaceView) -> SurfaceView:
        return nexts[cand.label]

    outcome = await mpc_run(
        _surface("here"), model, _branch_fsm(), executor,
        surface_to_state=_state_of, replan_budget=1,
    )
    assert outcome.reached_goal is True
    assert outcome.escalated is False
    assert outcome.rollbacks == 1
    assert [c.label for c in outcome.steps] == ["reach", "done"]


@pytest.mark.asyncio
async def test_mpc_run_escalates_when_budget_exhausted() -> None:
    # The model only ever proposes the trap move AND replan_budget=0 (the default):
    # the loop escalates immediately — the legacy escalate-on-trap behavior, no
    # rollback performed.
    model = ScriptedProvider(
        [ModelResponse(tool_calls=[_tc("wander", "a2")], finish=Finish.TOOL_CALLS)]
    )
    executed: list[str] = []

    async def executor(cand: Candidate, surface: SurfaceView) -> SurfaceView:
        executed.append(cand.label)
        return _surface("goal")

    outcome = await mpc_run(
        _surface("here"), model, _branch_fsm(), executor,
        surface_to_state=_state_of, replan_budget=0,
    )
    assert outcome.escalated is True
    assert outcome.rollbacks == 0
    assert executed == []


@pytest.mark.asyncio
async def test_mpc_rollback_never_recrosses_committing_edge() -> None:
    # After a trap the only untried sibling is a COMMITTING op ('pay'). The loop must
    # NOT execute it: it STOPS at the commit boundary and escalates, proving
    # consume-once is preserved (no committed side effect is ever re-run). Even with
    # budget the rollback can only re-try REVERSIBLE siblings.
    model = ScriptedProvider(
        [
            ModelResponse(tool_calls=[_tc("wander", "a2")], finish=Finish.TOOL_CALLS),
            ModelResponse(
                tool_calls=[ToolCall(name="pay", args={"label": "reach", "op": "pay"})],
                finish=Finish.TOOL_CALLS,
            ),
        ]
    )
    executed: list[str] = []

    async def executor(cand: Candidate, surface: SurfaceView) -> SurfaceView:
        executed.append(cand.label)
        return _surface("goal")

    outcome = await mpc_run(
        _surface("here"), model, _branch_fsm(), executor,
        surface_to_state=_state_of, replan_budget=2,
    )
    assert outcome.escalated is True
    assert executed == []  # the committing sibling never executed — consume-once kept
    assert outcome.rollbacks == 1  # rolled back once (the trap), then hit the commit


@pytest.mark.asyncio
async def test_mpc_run_excludes_already_tried_sibling() -> None:
    # After rolling back, the replan must NOT re-pick the tried trap label even if the
    # model re-emits it: 'wander' stays excluded, so a model that keeps proposing it
    # cannot loop forever — the budget bounds it and it escalates (no progress).
    model = ScriptedProvider(
        [
            ModelResponse(tool_calls=[_tc("wander", "a2")], finish=Finish.TOOL_CALLS),
            ModelResponse(tool_calls=[_tc("wander", "a2")], finish=Finish.TOOL_CALLS),
        ]
    )

    async def executor(cand: Candidate, surface: SurfaceView) -> SurfaceView:
        return _surface("goal")

    outcome = await mpc_run(
        _surface("here"), model, _branch_fsm(), executor,
        surface_to_state=_state_of, replan_budget=1,
    )
    # one rollback, then the re-proposed 'wander' is excluded ⇒ no on-rail move ⇒
    # escalate (terminates; never an infinite loop).
    assert outcome.escalated is True
    assert outcome.rollbacks == 1
    assert [c.label for c in outcome.steps] == []


# --- #35: optional per-run dead-edge mask in the live seam -----------------


def _branch_fsm_two_routes() -> Fsm:
    # here has TWO on-rail edges to goal-reachable states:
    #   here --reach--> good  --done--> goal
    #   here --reach2--> good2 --done2--> goal
    return Fsm(
        states=frozenset({"here", "good", "good2", "goal", "dead"}),
        initial="here",
        accepting=frozenset({"goal"}),
        edges=(
            FsmEdge("here", "good", "reach"),
            FsmEdge("good", "goal", "done"),
            FsmEdge("here", "good2", "reach2"),
            FsmEdge("good2", "goal", "done2"),
            FsmEdge("here", "dead", "wander"),
        ),
    )


@pytest.mark.asyncio
async def test_mpc_step_masks_dead_edge_candidate() -> None:
    # The model proposes the on-rail 'reach', but ('here','reach') is masked dead for
    # THIS run ⇒ it is scored off-rail and never chosen; the only move is masked ⇒
    # escalate. The mask is per-call — the FSM is never mutated.
    model = ScriptedProvider(
        [ModelResponse(tool_calls=[_tc("reach", "a1")], finish=Finish.TOOL_CALLS)]
    )
    decision = await live_mpc_step(
        _surface("here"), model, _branch_fsm(), surface_to_state=_state_of,
        dead_edges=frozenset({("here", "reach")}),
    )
    assert decision.escalate is True
    assert decision.action is None


@pytest.mark.asyncio
async def test_mpc_step_dead_edge_default_empty_unchanged() -> None:
    # The existing pick-on-rail scenario with dead_edges omitted still picks 'reach'
    # — default-empty is a no-op.
    model = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[_tc("wander", "a2"), _tc("reach", "a1")],
                finish=Finish.TOOL_CALLS,
            )
        ]
    )
    decision = await live_mpc_step(
        _surface("here"), model, _branch_fsm(), surface_to_state=_state_of
    )
    assert decision.escalate is False
    assert decision.action is not None and decision.action.label == "reach"


@pytest.mark.asyncio
async def test_mpc_step_dead_edge_routes_to_other_candidate() -> None:
    # 'here' has two on-rail siblings ('reach', 'reach2'); the model proposes both.
    # Masking ('here','reach') routes around it to the still-valid 'reach2' rather
    # than escalating.
    model = ScriptedProvider(
        [
            ModelResponse(
                tool_calls=[_tc("reach", "a1"), _tc("reach2", "a1")],
                finish=Finish.TOOL_CALLS,
            )
        ]
    )
    decision = await live_mpc_step(
        _surface("here"), model, _branch_fsm_two_routes(), surface_to_state=_state_of,
        dead_edges=frozenset({("here", "reach")}),
    )
    assert decision.escalate is False
    assert decision.action is not None and decision.action.label == "reach2"


@pytest.mark.asyncio
async def test_mpc_run_threads_dead_edges() -> None:
    # mpc_run forwards the mask into live_mpc_step: masking the first step's edge ⇒
    # no on-rail move ⇒ escalate, and the executor never runs across a masked edge.
    model = ScriptedProvider(
        [ModelResponse(tool_calls=[_tc("reach", "a1")], finish=Finish.TOOL_CALLS)]
    )
    executed: list[str] = []

    async def executor(cand: Candidate, surface: SurfaceView) -> SurfaceView:
        executed.append(cand.label)
        return _surface("good")

    outcome = await mpc_run(
        _surface("here"), model, _branch_fsm(), executor,
        surface_to_state=_state_of, dead_edges=frozenset({("here", "reach")}),
    )
    assert outcome.escalated is True
    assert executed == []


@pytest.mark.asyncio
async def test_mpc_step_dead_edge_does_not_mutate_fsm() -> None:
    # The mask is read-only over the FSM — running live_mpc_step with a dead_edges
    # mask leaves fsm.states / fsm.edges unchanged (nothing persisted into the
    # learned model).
    fsm = _branch_fsm()
    states_before = fsm.states
    edges_before = fsm.edges
    model = ScriptedProvider(
        [ModelResponse(tool_calls=[_tc("reach", "a1")], finish=Finish.TOOL_CALLS)]
    )
    await live_mpc_step(
        _surface("here"), model, fsm, surface_to_state=_state_of,
        dead_edges=frozenset({("here", "reach")}),
    )
    assert fsm.states == states_before
    assert fsm.edges == edges_before


# --- PART B: the Shadow-sourced transition model --------------------------


def _shadow_recording(names: list[str]) -> list[SimpleNamespace]:
    # a synthetic recording: a navigate, then a click per name.
    out = [_ev(ev.SHADOW_USER_NAVIGATE, {"url": "https://x/start"})]
    for n in names:
        out.append(_ev(ev.SHADOW_USER_CLICK, {"target": {"name": n}}))
    return out


def test_fsm_from_shadow_folds_a_recording() -> None:
    fsm = fsm_from_shadow(_shadow_recording(["login", "search"]))
    assert fsm.initial == "shadow_start"
    assert "shadow_goal" in fsm.accepting
    labels = [e.label for e in fsm.edges]
    assert "navigate" in labels
    assert "click:login" in labels
    assert "click:search" in labels
    assert "done" in labels


def test_fsm_from_shadow_accepts_an_induced_fsm_directly() -> None:
    # the synthesizer already emits a reachability.Fsm — consume it as plain input
    # (no zu-shadow import).
    induced = fsm_from_shadow_events(_shadow_recording(["a"]))
    same = fsm_from_shadow(induced)
    assert same is induced  # passed through unchanged


def test_second_recording_grows_the_graph() -> None:
    first = fsm_from_shadow(_shadow_recording(["login"]))
    # a DIFFERENT recording, folded into a disjoint state space, then merged.
    second = fsm_from_shadow_events(
        _shadow_recording(["pay"]), initial="r2_start", goal="r2_goal"
    )
    grown = fsm_from_shadow(second, base=first)
    # the merged graph contains BOTH recordings' edges — accumulation GROWS it.
    labels = {e.label for e in grown.edges}
    assert "click:login" in labels
    assert "click:pay" in labels
    assert grown.states >= first.states
    assert len(grown.edges) > len(first.edges)


def test_fsm_from_shadow_takes_recordedsession_shaped_object() -> None:
    # a RecordedSession-shaped duck (exposes shadow_events()) — folded WITHOUT
    # importing zu-shadow.
    evs = _shadow_recording(["x"])
    session = SimpleNamespace(shadow_events=lambda: evs)
    fsm = fsm_from_shadow(session)
    assert "click:x" in {e.label for e in fsm.edges}


def test_merge_is_source_agnostic_event_log_and_shadow() -> None:
    # a Shadow-induced FSM and any other induced FSM feed the SAME model.
    a = fsm_from_shadow_events(_shadow_recording(["one"]), initial="a0", goal="ag")
    b = fsm_from_shadow_events(_shadow_recording(["two"]), initial="b0", goal="bg")
    merged = merge_transition_models(a, b)
    assert merged.initial == "a0"
    assert {"ag", "bg"} <= merged.accepting
