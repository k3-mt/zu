"""Offline guided search — best-first planning OVER the Phase-1 induced FSM.

This is the planner half of the §5 stack (the policy prior is the recognizer).
It runs over ``zu_core.reachability.Fsm`` (REUSE — there is no second FSM type)
with the pattern recognizer as the move-ordering prior, the same AlphaZero shape
the doc describes: explore the residual the pattern does not resolve, guided by
``co_reachable`` (a cheap value estimate) and pruned of ``trap_states``.

Two pieces:
  * ``fsm_from_events`` — an EMPIRICAL transition-model builder: fold the event
    log's surface→action→surface triples into FSM edges (the documented future
    ``fsm_from_track`` helper, sourced from the event log NOW; Shadow recordings
    EXTEND this later — DEFERRED, see below).
  * ``plan`` — best-first search over the FSM, ordered by
    ``f = co-reachability + prior``, pruning edges into traps, and FLAGGING which
    edges cross a committing boundary (so the deferred live executor knows where
    lookahead must stop). Offline the whole learned graph is explorable; the plan
    never auto-crosses a COMMITTING edge in the live seam.

Pure, offline, $0. The LIVE guided-MPC loop and the Shadow-sourced transition
model are DEFERRED seams (``live_mpc_step`` is a documented stub).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from zu_core import events as ev
from zu_core.reachability import Fsm, FsmEdge, co_reachable, trap_states

from .reversibility import Commitment

# --- (A) the empirical transition-model builder ---------------------------


def _payload(e: Any) -> dict:
    p = getattr(e, "payload", None)
    return p if isinstance(p, dict) else {}


def surface_state_id(payload: dict) -> str:
    """A stable digest of the surface the agent was on — the FSM state id.

    Heuristic (documented): prefer ``url`` + ``title`` when present (a web locus);
    otherwise hash the sorted affordance handles (the structural fingerprint of
    the surface). Two visits to the same page collapse to the same state; two
    structurally different surfaces stay distinct. Shadow recordings (next phase)
    supply richer state; this is the event-log source.
    """
    url = str(payload.get("url", ""))
    title = str(payload.get("title", ""))
    if url or title:
        basis = f"url={url}\x1ftitle={title}"
    else:
        handles = payload.get("handles") or []
        basis = "h=" + ",".join(sorted(str(h) for h in handles))
    return "s_" + hashlib.sha256(basis.encode()).hexdigest()[:12]


def fsm_from_events(
    events: Sequence[Any],
    *,
    goal_states: frozenset[str] | None = None,
    initial: str | None = None,
) -> Fsm:
    """Fold an event log into an induced ``Fsm`` (pure).

    The log is read as a sequence of ``data.surface.captured`` snapshots
    interleaved with ``harness.tool.invoked`` actions: each
    surface→tool→next-surface triple becomes an edge
    ``FsmEdge(src=state_before, dst=state_after, label=action)``. The action
    label is the tool name (plus handle when present), so the edge names the move
    that induced the transition. Accepting states are ``goal_states`` (the caller
    supplies them — e.g. states where a success Monitor would hold).
    """
    states: list[str] = []
    edges: list[FsmEdge] = []
    last_state: str | None = None
    pending_action: str | None = None
    for e in events:
        etype = getattr(e, "type", None)
        if etype == ev.SURFACE_CAPTURED:
            sid = surface_state_id(_payload(e))
            if sid not in states:
                states.append(sid)
            if last_state is not None and pending_action is not None:
                edges.append(FsmEdge(src=last_state, dst=sid, label=pending_action))
            last_state = sid
            pending_action = None
        elif etype == ev.TOOL_INVOKED:
            p = _payload(e)
            tool = str(p.get("tool", "action"))
            handle = p.get("handle") or (p.get("args") or {}).get("handle")
            pending_action = f"{tool}:{handle}" if handle else tool
    init = initial if initial is not None else (states[0] if states else "")
    accepting = goal_states if goal_states is not None else frozenset()
    # Ensure declared goal states are part of the state set (a goal may be named
    # before it is observed).
    state_set = frozenset(states) | accepting | ({init} if init else frozenset())
    return Fsm(states=state_set, initial=init, accepting=accepting & state_set, edges=tuple(edges))


# --- (B) the best-first planner over the induced FSM ----------------------


@dataclass(frozen=True)
class PlanStep:
    """One move in a plan: the edge taken, and whether it crosses a commit
    boundary (the live executor must STOP — never auto-cross — a committing edge)."""

    src: str
    dst: str
    label: str
    committing: bool


@dataclass(frozen=True)
class Plan:
    """A planned path from the FSM's initial state toward a goal.

    ``reached_goal`` says whether the path ends in an accepting state.
    ``steps`` is the ordered moves; ``crosses_commit`` flags whether any step is a
    committing boundary (the deferred live MPC must halt before it). ``expansions``
    records search effort for the $0 test bar.
    """

    steps: tuple[PlanStep, ...]
    reached_goal: bool
    crosses_commit: bool
    expansions: int = 0
    detail: str | None = None


# A prior over an edge: returns a non-negative bonus (higher ⇒ explore first).
EdgePrior = Callable[[FsmEdge], float]
# A commitment classifier over an edge: REVERSIBLE | COMMITTING.
EdgeClassifier = Callable[[FsmEdge], Commitment]


def _default_classifier(edge: FsmEdge) -> Commitment:
    # OFFLINE-EXPLORATION-ONLY. This REVERSIBLE default is the inverse of the
    # project-wide default-to-committing rail discipline, and that is DELIBERATE
    # AND SAFE *because it never gates a live side-effecting action*:
    #   * ``plan()`` runs purely offline over the learned/remembered FSM. Marking
    #     an unknown edge REVERSIBLE only lets the planner LOOK PAST it; it does not
    #     execute anything. The commit boundary is FLAGGED on each ``PlanStep``
    #     (``committing``) and aggregated in ``Plan.crosses_commit`` — surfaced, not
    #     crossed.
    #   * The LIVE seam (``live_mpc_step``, deferred) does NOT trust this default:
    #     it re-classifies every candidate edge with ``reversibility.classify_action``,
    #     which DEFAULTS TO COMMITTING on uncertainty (see
    #     ``test_live_classifier_defaults_to_committing``), and STOPS at the first
    #     committing boundary. So the offline REVERSIBLE default cannot leak into a
    #     live execution decision.
    # If that separation could ever be violated, flip this to COMMITTING.
    return Commitment.REVERSIBLE


def _default_prior(edge: FsmEdge) -> float:
    return 0.0


def plan(
    fsm: Fsm,
    *,
    prior: EdgePrior = _default_prior,
    classifier: EdgeClassifier = _default_classifier,
    max_expansions: int = 1000,
) -> Plan:
    """Best-first search from ``fsm.initial`` toward an accepting state (pure).

    The frontier is ordered by ``f = co_reachability(dst) + prior(edge)``: an edge
    whose destination can still reach the goal, preferred by the move-ordering
    prior, is expanded first. Edges into trap states are PRUNED (reuse
    ``trap_states``). Each chosen edge is classified; a committing edge is FLAGGED
    in the plan (offline we still record the path, but ``crosses_commit`` tells the
    deferred live executor where lookahead must stop).
    """
    co = co_reachable(fsm)
    traps = trap_states(fsm)
    start = fsm.initial
    if start not in fsm.states:
        return Plan(steps=(), reached_goal=False, crosses_commit=False, detail="no initial state")

    # best-first over partial paths. A path is (steps, current_state, visited).
    @dataclass(order=True)
    class _Node:
        score: float
        seq: int  # tie-breaker for determinism
        steps: tuple[PlanStep, ...] = field(compare=False)
        state: str = field(compare=False)
        visited: frozenset[str] = field(compare=False)

    import heapq

    counter = 0
    heap: list[_Node] = [_Node(score=0.0, seq=0, steps=(), state=start, visited=frozenset({start}))]
    best_partial: tuple[PlanStep, ...] = ()
    expansions = 0
    while heap and expansions < max_expansions:
        node = heapq.heappop(heap)
        expansions += 1
        if node.state in fsm.accepting:
            return Plan(
                steps=node.steps,
                reached_goal=True,
                crosses_commit=any(s.committing for s in node.steps),
                expansions=expansions,
            )
        if len(node.steps) > len(best_partial):
            best_partial = node.steps
        # expand: outgoing edges, skipping traps and already-visited states.
        out = sorted(
            (e for e in fsm.edges if e.src == node.state),
            key=lambda e: (-(float(e.dst in co) + prior(e)), e.label),
        )
        for e in out:
            if e.dst in traps or e.dst in node.visited:
                continue
            committing = classifier(e) is Commitment.COMMITTING
            step = PlanStep(src=e.src, dst=e.dst, label=e.label, committing=committing)
            # f = negative so heapq (a min-heap) pops the highest-value first.
            f = -(float(e.dst in co) + prior(e))
            counter += 1
            heapq.heappush(
                heap,
                _Node(
                    score=f,
                    seq=counter,
                    steps=node.steps + (step,),
                    state=e.dst,
                    visited=node.visited | {e.dst},
                ),
            )
    return Plan(
        steps=best_partial,
        reached_goal=False,
        crosses_commit=any(s.committing for s in best_partial),
        expansions=expansions,
        detail="goal not reached within max_expansions" if heap else "frontier exhausted",
    )


# --- DEFERRED: the live guided-MPC seam (documented, not built) -----------


def live_mpc_step(*_args: Any, **_kwargs: Any) -> None:
    """DEFERRED — the live guided-MPC loop (model proposes K → shallow live
    lookahead over the REVERSIBLE sub-graph → execute one → re-plan).

    The doc marks this "(optional, later)". The offline planner above is the
    pathfinder; the live executor would call ``plan`` for K candidates, run a
    shallow lookahead that STOPS at the first ``PlanStep.committing`` boundary
    (never auto-crossing an irreversible commit), execute one reversible step,
    observe, and re-plan. It also needs the Shadow-sourced transition model (the
    next phase) to extend ``fsm_from_events`` with richer state. Both are
    intentionally NOT built in this phase.
    """
    raise NotImplementedError(
        "live_mpc_step is a deferred seam (live guided-MPC + Shadow-sourced "
        "transition model); use plan()/fsm_from_events() for the offline planner."
    )
