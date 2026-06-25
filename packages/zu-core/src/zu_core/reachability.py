"""Reachability over an induced finite-state machine (the §1.7 rail check).

A Monitor folds the *actual* event log; this module reasons about the *possible*
futures of an induced plan. Given an :class:`Fsm` — a NEW branching structure,
deliberately NOT the linear :class:`zu_core.track.Track` ("a line, not a map") —
:func:`check_reachability` answers the deterministic question a planner/rail
guard needs: *can this plan still reach the goal, and which states are dead ends
("traps") from which the goal is unreachable?*

It is a pure library: stdlib + pydantic only, no model, no I/O. The deterministic
machinery DISPOSES — a trap-state verdict is a fact about the graph, computed by a
backward fixpoint from the accepting states, never a model's opinion. The FSM is
hand-built today (and in tests); a §2 synthesizer will later produce it from a
Track via a separate, additive ``fsm_from_track`` helper, and this checker
consumes it unchanged.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from pydantic import BaseModel


@dataclass(frozen=True)
class FsmEdge:
    """A directed transition ``src --label--> dst``. ``label`` names the
    action/tool that induces the transition (empty when irrelevant)."""

    src: str
    dst: str
    label: str = ""


@dataclass
class Fsm:
    """An induced finite-state machine: states, one initial state, a set of
    accepting/goal states, and labelled directed edges.

    A branching transition system — multiple edges may leave a state — which is
    what distinguishes it from the linear :class:`Track`. The helpers are pure
    graph queries; the reachability functions below build on them.
    """

    states: frozenset[str]
    initial: str
    accepting: frozenset[str]
    edges: tuple[FsmEdge, ...] = field(default_factory=tuple)

    def successors(self, s: str) -> frozenset[str]:
        """The states directly reachable from ``s`` by one edge."""
        return frozenset(e.dst for e in self.edges if e.src == s)

    def predecessors(self, s: str) -> frozenset[str]:
        """The states that reach ``s`` directly by one edge."""
        return frozenset(e.src for e in self.edges if e.dst == s)


def _reachable_forward(fsm: Fsm) -> frozenset[str]:
    """Forward BFS from ``initial``: every state reachable by following edges."""
    seen: set[str] = {fsm.initial} if fsm.initial in fsm.states else set()
    queue: deque[str] = deque(seen)
    while queue:
        s = queue.popleft()
        for nxt in fsm.successors(s):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return frozenset(seen)


def co_reachable(fsm: Fsm) -> frozenset[str]:
    """The set of states from which SOME accepting state is reachable.

    A backward BFS/fixpoint from the accepting states over reversed edges: seed
    with the accepting states, then repeatedly add any predecessor of an already
    co-reachable state until no more can be added. Pure function — depends only
    on the graph. With no accepting states the result is empty (every state is a
    trap), the degenerate guard.
    """
    seen: set[str] = {s for s in fsm.accepting if s in fsm.states}
    queue: deque[str] = deque(seen)
    while queue:
        s = queue.popleft()
        for pred in fsm.predecessors(s):
            if pred not in seen:
                seen.add(pred)
                queue.append(pred)
    return frozenset(seen)


def trap_states(fsm: Fsm) -> frozenset[str]:
    """States that cannot reach any accepting state — the complement of the
    co-reachable set within the FSM's states. Each is a dead end for the plan."""
    return fsm.states - co_reachable(fsm)


class ReachabilityVerdict(BaseModel):
    """The deterministic 'can this induced plan still reach the goal' verdict."""

    # Is the initial state co-reachable (the goal still reachable from the start)?
    reachable_goal: bool
    # Trap states (cannot reach the goal), sorted for a stable record.
    traps: list[str]
    # Forward-unreachable (dead) states never reached from the initial state,
    # sorted — reported for completeness, distinct from traps.
    unreachable_from_initial: list[str]


def check_reachability(fsm: Fsm) -> ReachabilityVerdict:
    """Compute the reachability verdict for an induced FSM (pure).

    ``reachable_goal`` is ``fsm.initial in co_reachable(fsm)``; ``traps`` is the
    sorted trap-state set; ``unreachable_from_initial`` is the sorted set of
    states never reached by following edges forward from the initial state.
    """
    co = co_reachable(fsm)
    forward = _reachable_forward(fsm)
    return ReachabilityVerdict(
        reachable_goal=fsm.initial in co,
        traps=sorted(trap_states(fsm)),
        unreachable_from_initial=sorted(fsm.states - forward),
    )
