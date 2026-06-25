"""Offline guided search + the event-log → FSM transition builder — pure, $0."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from zu_core import events as ev
from zu_core.reachability import Fsm, FsmEdge
from zu_patterns.reversibility import Commitment, classify_action
from zu_patterns.search import (
    Plan,
    PlanStep,
    _default_classifier,
    fsm_from_events,
    plan,
    surface_state_id,
)


def _ev(etype: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type=etype, payload=payload)


# --- (A) the transition-model builder -------------------------------------


def test_fsm_from_events_folds_surface_action_surface() -> None:
    events = [
        _ev(ev.SURFACE_CAPTURED, {"url": "https://x/list", "title": "List"}),
        _ev(ev.TOOL_INVOKED, {"tool": "action_surface", "args": {"handle": "a2"}}),
        _ev(ev.SURFACE_CAPTURED, {"url": "https://x/item", "title": "Item"}),
    ]
    s0 = surface_state_id({"url": "https://x/list", "title": "List"})
    s1 = surface_state_id({"url": "https://x/item", "title": "Item"})
    fsm = fsm_from_events(events, goal_states=frozenset({s1}))
    assert fsm.initial == s0
    assert s1 in fsm.accepting
    assert FsmEdge(src=s0, dst=s1, label="action_surface:a2") in fsm.edges


def test_surface_state_id_collapses_same_page() -> None:
    a = surface_state_id({"url": "https://x/p", "title": "P"})
    b = surface_state_id({"url": "https://x/p", "title": "P"})
    c = surface_state_id({"handles": ["a1", "a2"]})
    assert a == b
    assert a != c  # different basis ⇒ different state


# --- (B) the best-first planner -------------------------------------------


def _line_fsm() -> Fsm:
    # s0 -> s1 -> goal, with a trap branch s0 -> dead.
    return Fsm(
        states=frozenset({"s0", "s1", "goal", "dead"}),
        initial="s0",
        accepting=frozenset({"goal"}),
        edges=(
            FsmEdge("s0", "s1", "go"),
            FsmEdge("s1", "goal", "finish"),
            FsmEdge("s0", "dead", "wander"),
        ),
    )


def test_plan_reaches_goal_and_prunes_trap() -> None:
    p = plan(_line_fsm())
    assert isinstance(p, Plan)
    assert p.reached_goal is True
    labels = [s.label for s in p.steps]
    assert labels == ["go", "finish"]
    # the trap branch ("wander" into "dead") is never taken.
    assert "wander" not in labels


def test_plan_prior_orders_moves() -> None:
    # two paths to goal; the prior favours the "fast" edge.
    fsm = Fsm(
        states=frozenset({"s0", "a", "b", "goal"}),
        initial="s0",
        accepting=frozenset({"goal"}),
        edges=(
            FsmEdge("s0", "a", "slow"),
            FsmEdge("s0", "b", "fast"),
            FsmEdge("a", "goal", "x"),
            FsmEdge("b", "goal", "y"),
        ),
    )
    p = plan(fsm, prior=lambda e: 1.0 if e.label == "fast" else 0.0)
    assert p.reached_goal is True
    assert p.steps[0].label == "fast"


def test_plan_flags_committing_edge() -> None:
    fsm = _line_fsm()

    def classifier(e: FsmEdge) -> Commitment:
        return Commitment.COMMITTING if e.label == "finish" else Commitment.REVERSIBLE

    p = plan(fsm, classifier=classifier)
    assert p.reached_goal is True
    assert p.crosses_commit is True
    commit_steps = [s for s in p.steps if s.committing]
    assert len(commit_steps) == 1 and commit_steps[0].label == "finish"


def test_plan_no_goal_returns_best_partial() -> None:
    # no accepting state ⇒ everything is a trap; no path, not crashing.
    fsm = Fsm(
        states=frozenset({"s0", "s1"}),
        initial="s0",
        accepting=frozenset(),
        edges=(FsmEdge("s0", "s1", "go"),),
    )
    p = plan(fsm)
    assert p.reached_goal is False


def test_plan_step_is_frozen() -> None:
    step = PlanStep(src="s0", dst="s1", label="go", committing=False)
    with pytest.raises((AttributeError, TypeError)):
        step.label = "x"  # type: ignore[misc]


# --- the commit-boundary safety discipline (the LOW fix) ------------------


def test_offline_default_classifier_is_reversible_exploration_only() -> None:
    # The OFFLINE planner's default for an unknown edge is REVERSIBLE — explorable.
    # This is the inverse of the rail default ON PURPOSE: it only lets the planner
    # LOOK PAST an edge during $0 offline search; it never executes anything.
    assert _default_classifier(FsmEdge("s0", "s1", "unknown")) is Commitment.REVERSIBLE


def test_live_classifier_defaults_to_committing() -> None:
    # The LIVE seam re-classifies every candidate with classify_action, which
    # DEFAULTS TO COMMITTING on uncertainty — so the offline REVERSIBLE default
    # cannot leak into a live side-effecting decision. (Named in search.py's
    # _default_classifier comment as the proof of the separation.)
    assert classify_action() is Commitment.COMMITTING  # no signal at all
    assert classify_action(role="button") is Commitment.COMMITTING  # ambiguous button
    assert classify_action(op="totally-unknown-op") is Commitment.COMMITTING
