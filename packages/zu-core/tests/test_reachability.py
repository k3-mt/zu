"""ZU-RAIL-7 — pure reachability over an induced FSM (the branching structure,
NOT the linear Track). Hand-built FSMs, no model, no loop, no network.

co-reachability = a backward fixpoint from the accepting/goal states; a trap is a
state that cannot reach the goal. The verdict is a fact about the graph.
"""

from __future__ import annotations

from zu_core.reachability import (
    Fsm,
    FsmEdge,
    check_reachability,
    co_reachable,
    trap_states,
)


def test_co_reachability_from_goal() -> None:
    fsm = Fsm(
        states=frozenset({"A", "B", "GOAL"}),
        initial="A",
        accepting=frozenset({"GOAL"}),
        edges=(FsmEdge("A", "B"), FsmEdge("B", "GOAL")),
    )
    assert co_reachable(fsm) == frozenset({"A", "B", "GOAL"})
    v = check_reachability(fsm)
    assert v.reachable_goal is True
    assert v.traps == []


def test_trap_state_detected() -> None:
    # A->B->GOAL, plus A->T where T is a sink with no edge out: T cannot reach GOAL.
    fsm = Fsm(
        states=frozenset({"A", "B", "GOAL", "T"}),
        initial="A",
        accepting=frozenset({"GOAL"}),
        edges=(FsmEdge("A", "B"), FsmEdge("B", "GOAL"), FsmEdge("A", "T")),
    )
    assert trap_states(fsm) == frozenset({"T"})
    v = check_reachability(fsm)
    assert v.traps == ["T"]
    # The initial still reaches GOAL via B, so the goal is reachable overall.
    assert v.reachable_goal is True


def test_initial_in_trap_means_goal_unreachable() -> None:
    # GOAL has no inbound edge from the initial's component: initial cannot reach it.
    fsm = Fsm(
        states=frozenset({"A", "B", "GOAL"}),
        initial="A",
        accepting=frozenset({"GOAL"}),
        edges=(FsmEdge("A", "B"), FsmEdge("B", "A")),  # A<->B cycle, GOAL isolated
    )
    v = check_reachability(fsm)
    assert v.reachable_goal is False
    assert "A" in v.traps and "B" in v.traps


def test_unreachable_from_initial_reported() -> None:
    # ISO is never reached forward from A, though it can reach GOAL (so not a trap).
    fsm = Fsm(
        states=frozenset({"A", "GOAL", "ISO"}),
        initial="A",
        accepting=frozenset({"GOAL"}),
        edges=(FsmEdge("A", "GOAL"), FsmEdge("ISO", "GOAL")),
    )
    v = check_reachability(fsm)
    assert v.unreachable_from_initial == ["ISO"]
    assert "ISO" not in v.traps  # ISO reaches GOAL, so it is co-reachable


def test_empty_accepting_is_all_traps() -> None:
    # Degenerate guard: no accepting states => every state is a trap, no crash.
    fsm = Fsm(
        states=frozenset({"A", "B"}),
        initial="A",
        accepting=frozenset(),
        edges=(FsmEdge("A", "B"),),
    )
    assert co_reachable(fsm) == frozenset()
    assert trap_states(fsm) == frozenset({"A", "B"})
    v = check_reachability(fsm)
    assert v.reachable_goal is False
    assert v.traps == ["A", "B"]
