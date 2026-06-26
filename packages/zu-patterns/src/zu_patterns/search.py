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

Now also:
  * ``live_mpc_step`` / ``mpc_run`` — the LIVE guided-MPC loop (§5.2, the
    AlphaZero shape): the model PROPOSES ≤K candidates (the recognizer is the
    move-ordering prior), a shallow lookahead over the learned FSM DISPOSES via
    the rail (``co_reachable``/traps), one REVERSIBLE step executes via an injected
    executor, then re-plan — STOPPING at the commit boundary (default-to-committing).
  * ``fsm_from_shadow`` / ``merge_transition_models`` — the Shadow-sourced
    transition model: fold a recording's induced FSM / shadow events into the SAME
    search model; accumulating recordings GROWS the graph.

Pure, offline, $0 — the executor is the only I/O and it is injected (a fake in
tests, a real browser in production).
"""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from zu_core import events as ev
from zu_core.content_view import ContentView
from zu_core.escalation import ProblemContext, Repair
from zu_core.ports import ModelProvider, ModelRequest, RecognitionResult
from zu_core.reachability import Fsm, FsmEdge, co_reachable, trap_states
from zu_core.surface import SurfaceAffordance, SurfaceView

from .recognizer import recognize
from .reversibility import Commitment, classify_action

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
# A per-run masked edge: a ``(state_id, edge_label)`` pair PROVEN DEAD this run.
# The mask is read-only over the passed-in ``Fsm`` and is NEVER persisted into the
# learned FSM — it routes the search around an edge for THIS call/run only (a
# dynamically-discovered trap). NOTE: the key is ``(state_id, edge_label)`` — the
# only key usable in BOTH ``plan`` and ``live_mpc_step`` (the latter knows a
# candidate's ``label`` but not its destination). In the induced FSM an edge label
# is an action name (tool:handle / verb:target), normally unique per state, but a
# label collision out of one state would mask all such edges (a known limitation).
DeadEdge = tuple[str, str]


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
    dead_edges: frozenset[DeadEdge] = frozenset(),
) -> Plan:
    """Best-first search from ``fsm.initial`` toward an accepting state (pure).

    The frontier is ordered by ``f = co_reachability(dst) + prior(edge)``: an edge
    whose destination can still reach the goal, preferred by the move-ordering
    prior, is expanded first. Edges into trap states are PRUNED (reuse
    ``trap_states``). Each chosen edge is classified; a committing edge is FLAGGED
    in the plan (offline we still record the path, but ``crosses_commit`` tells the
    deferred live executor where lookahead must stop).

    ``dead_edges`` is an OPTIONAL per-call/per-run mask of ``(state_id, edge_label)``
    pairs proven dead THIS run: a masked edge is skipped during expansion exactly
    like a trap, so the search ROUTES AROUND it. The mask is read-only — it is NEVER
    persisted into ``fsm`` (the learned FSM is not mutated); it lasts only for this
    call. Default empty (a no-op, fully backwards-compatible).
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
            # per-run dead-edge mask: a (state, label) proven dead THIS run is
            # routed around exactly like a trap (no fsm mutation — mask is read-only).
            if (e.src, e.label) in dead_edges:
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


# --- (C) the LIVE guided-MPC loop (§5.2, the AlphaZero shape) --------------
#
# MODEL PROPOSES, HARNESS DISPOSES. The model proposes ≤K candidate next actions
# (policy-pruned branching) — the pattern recognizer supplies the move-ordering
# PRIOR; a shallow lookahead over the LEARNED ``Fsm`` (the remembered transition
# model) estimates where each candidate leads; the rail/reachability DISPOSES
# (co_reachable to the goal? not a trap?). A pattern's prediction is a PRIOR
# confirmed by the deterministic lookahead/rail, NEVER ground truth.
#
# ``live_mpc_step`` is PURE decision logic — no real I/O. The executor is injected
# into the driver loop (``mpc_run``), so the whole thing is offline-testable with a
# ScriptedProvider + a hand-built Fsm + a fake executor.


# A proposed candidate: the action label (matching an FSM edge ``label``), the
# affordance handle it acts on (for the executor), and a generic interaction verb
# (``op``)/``role`` the commit-boundary classifier reads.
@dataclass(frozen=True)
class Candidate:
    label: str
    handle: str | None = None
    op: str | None = None
    role: str | None = None
    http_method: str | None = None


@dataclass(frozen=True)
class MpcDecision:
    """The result of one ``live_mpc_step``: the chosen candidate and WHY.

    ``action`` is the picked on-rail candidate (``None`` ⇒ no on-rail/safe move —
    the loop escalates). ``escalate`` is set when the best candidate crosses the
    COMMIT BOUNDARY (a side-effecting/irreversible step the live loop must NOT
    auto-cross) or when nothing is recognized/reachable. ``committing`` says the
    chosen/blocking candidate was classified COMMITTING. ``scored`` is the full
    ranked list (candidate, lookahead-score) for audit — the lookahead+rail
    DISPOSED, the model only PROPOSED.
    """

    action: Candidate | None
    escalate: bool
    rationale: str
    committing: bool = False
    scored: tuple[tuple[Candidate, float], ...] = ()


# The state of the current surface within the learned FSM. The caller maps a live
# ``SurfaceView`` to an FSM state id; offline tests pass the id directly.
SurfaceToState = Callable[[SurfaceView], str]


def _surface_state(surface: SurfaceView) -> str:
    """Default surface→FSM-state mapping: the same digest ``fsm_from_events``
    uses, so a live surface lands on the learned state when the model remembers it."""
    payload = {
        "url": surface.url,
        "title": surface.title,
        "handles": [a.handle for a in surface.affordances],
    }
    return surface_state_id(payload)


def _prior_for_candidate(
    cand: Candidate, recognized: RecognitionResult | None
) -> float:
    """The move-ordering PRIOR (the recognizer's confidence, biased to the handles
    the recognized archetype bound). A recognized handle ⇒ explore-first bonus."""
    if recognized is None:
        return 0.0
    bonus = recognized.confidence
    if cand.handle is not None and cand.handle in recognized.matched_handles:
        bonus += 1.0
    return bonus


def _lookahead_score(fsm: Fsm, co: frozenset[str], traps: frozenset[str],
                     dst: str, depth: int) -> float:
    """SHALLOW lookahead over the LEARNED fsm: how good is landing on ``dst``?

    The rail evaluator (``co_reachable``) is the value estimate: ``dst`` accepting
    ⇒ best; ``dst`` co-reachable (goal still reachable) ⇒ good; a trap ⇒ worst
    (pruned). Within ``depth`` we look whether an accepting state is reachable from
    ``dst`` (a cheap bounded BFS), preferring the shorter route. Pure graph query —
    no model, no I/O. This is what DISPOSES."""
    if dst in traps:
        return -1.0
    if dst in fsm.accepting:
        return 100.0
    if dst not in co:
        # not co-reachable and not accepting: a dead end for the goal.
        return -1.0
    # bounded BFS to the nearest accepting state within ``depth`` — closer is
    # better (the value estimate the rail's co_reachable underwrites).
    frontier = {dst}
    seen = {dst}
    for d in range(1, max(depth, 1) + 1):
        nxt: set[str] = set()
        for s in frontier:
            for e in fsm.edges:
                if e.src == s and e.dst not in seen:
                    if e.dst in fsm.accepting:
                        return 100.0 - d
                    if e.dst in co:
                        nxt.add(e.dst)
                    seen.add(e.dst)
        frontier = nxt
        if not frontier:
            break
    # co-reachable but goal is beyond the horizon: still on-rail, mild positive.
    return 1.0


async def live_mpc_step(
    surface: SurfaceView,
    model: ModelProvider,
    fsm: Fsm,
    patterns: Sequence[Any] = (),
    *,
    k: int = 3,
    depth: int = 2,
    surface_to_state: SurfaceToState | None = None,
    priors: Sequence[Any] = (),
    min_confidence: float = 0.6,
    dead_edges: frozenset[DeadEdge] = frozenset(),
    exclude: frozenset[str] = frozenset(),
) -> MpcDecision:
    """One guided-MPC step — MODEL PROPOSES, deterministic lookahead+rail DISPOSES.

    PROPOSE: the ``ModelProvider`` proposes ≤K candidate next actions from the
    current ``SurfaceView`` (policy-pruned branching; K small). The pattern
    recognizer supplies the move-ordering PRIOR — recognized archetypes/handles are
    explored first (the heuristic network).

    LOOK AHEAD: each candidate maps to an FSM edge out of the current state; a
    SHALLOW lookahead over the LEARNED fsm estimates where it leads, SCORED by the
    rail evaluator (``co_reachable`` to the goal / not a ``trap``).

    DISPOSE: pick the best-scoring on-rail candidate. A pattern's prediction is a
    PRIOR confirmed by the lookahead/rail, never trusted as ground truth.

    SAFETY — STOP AT THE COMMIT BOUNDARY: the chosen candidate is re-classified by
    ``classify_action`` (default-to-COMMITTING on uncertainty). A COMMITTING next
    step is the live-search boundary: the step does NOT execute — the decision is
    ``escalate``. Only a REVERSIBLE/idempotent candidate is returned for execution.
    An UNRECOGNIZED / no-on-rail-candidate surface also escalates (fall through to
    the model / route out). Pure: no I/O beyond the injected ``model.complete``.

    ``dead_edges`` is an OPTIONAL per-call/per-run mask of ``(state_id, edge_label)``
    pairs proven dead THIS run: a candidate whose ``(here, label)`` is masked is
    scored OFF-RAIL (the same ``-2.0`` unknown-transition sentinel) so it can never
    be chosen, and a surface offering only masked moves escalates. The mask is
    read-only over ``fsm`` — NEVER persisted into the learned FSM; it lasts for this
    call only. ``exclude`` is the set of candidate labels already TRIED at this
    surface-state (used by ``mpc_run``'s structural rollback to replan a DIFFERENT
    on-rail sibling): an excluded label is likewise scored off-rail so the replan
    picks a genuinely different branch. Both default empty (a no-op).
    """
    to_state = surface_to_state or _surface_state
    here = to_state(surface)
    co = co_reachable(fsm)
    traps = trap_states(fsm)

    # PROPOSE — the model proposes ≤K candidates from the surface.
    rec = recognize(surface, patterns, min_confidence=min_confidence)
    proposals = await _propose_candidates(surface, model, rec.result, k=k)
    if not proposals:
        return MpcDecision(
            action=None, escalate=True,
            rationale="model proposed no candidates — escalate",
        )

    # LOOK AHEAD + score. Each candidate's label is matched to an outgoing FSM edge
    # from the current state (the learned transition); its destination is scored by
    # the rail. The recognizer's confidence is the move-ordering PRIOR (a tie-break
    # / bias, NEVER overriding the deterministic lookahead).
    edges_here = {e.label: e for e in fsm.edges if e.src == here}
    scored: list[tuple[Candidate, float]] = []
    for cand in proposals:
        # per-run dead-edge mask / already-tried sibling: a masked (here,label) or
        # an excluded label is treated as OFF-RAIL for THIS call (the -2.0 unknown-
        # transition sentinel), so the existing best_score<=0.0 gate disposes it —
        # routed around without mutating the learned fsm.
        if (here, cand.label) in dead_edges or cand.label in exclude:
            scored.append((cand, -2.0))
            continue
        edge = edges_here.get(cand.label)
        if edge is None:
            # the learned model has no memory of this move from here: unknown
            # transition ⇒ blind. Score it below any on-rail known move.
            scored.append((cand, -2.0))
            continue
        base = _lookahead_score(fsm, co, traps, edge.dst, depth)
        score = base + 0.001 * _prior_for_candidate(cand, rec.result)
        scored.append((cand, score))
    # deterministic ordering: score desc, then label for stable ties.
    scored.sort(key=lambda cs: (-cs[1], cs[0].label))
    scored_t = tuple(scored)

    best, best_score = scored[0]
    if best_score <= 0.0:
        # no on-rail candidate (trap / unknown / unreachable). The deterministic
        # lookahead+rail DISPOSED against the model's proposals — escalate rather
        # than execute a blind/off-rail move.
        return MpcDecision(
            action=None, escalate=True,
            rationale=(
                f"no on-rail candidate from {here!r} "
                f"(best {best.label!r} scored {best_score:.3f}) — escalate"
            ),
            scored=scored_t,
        )

    # DISPOSE — SAFETY: re-classify the chosen candidate at the COMMIT BOUNDARY.
    # default-to-committing: an uncertain/side-effecting move STOPS the live loop.
    commitment = classify_action(
        http_method=best.http_method, role=best.role, op=best.op, priors=priors
    )
    if commitment is Commitment.COMMITTING:
        return MpcDecision(
            action=best, escalate=True, committing=True,
            rationale=(
                f"chosen on-rail candidate {best.label!r} is COMMITTING "
                "(default-to-committing) — STOP at the commit boundary, escalate"
            ),
            scored=scored_t,
        )

    return MpcDecision(
        action=best, escalate=False, committing=False,
        rationale=(
            f"chosen {best.label!r} → on-rail (score {best_score:.3f}); "
            "REVERSIBLE — execute one step then re-plan"
        ),
        scored=scored_t,
    )


def _aff(surface: SurfaceView, handle: str | None) -> SurfaceAffordance | None:
    if handle is None:
        return None
    for a in surface.affordances:
        if a.handle == handle:
            return a
    return None


async def _propose_candidates(
    surface: SurfaceView, model: ModelProvider,
    recognized: RecognitionResult | None, *, k: int,
) -> list[Candidate]:
    """Ask the ModelProvider to PROPOSE ≤K candidate next actions over the surface.

    The model emits tool_calls (the policy-pruned branching factor): each call's
    ``args`` carry ``{label, handle?, op?, role?, http_method?}``. A proposal's
    ``op``/``role`` default from the named affordance when the model omits them, so
    the commit-boundary classifier always has signal. ≤K are kept (the model
    prunes; we cap)."""
    req = _proposal_request(surface, recognized, k=k)
    resp = await model.complete(req)
    out: list[Candidate] = []
    for call in resp.tool_calls[:k]:
        args = call.args or {}
        label = str(args.get("label") or call.name)
        handle = args.get("handle")
        aff = _aff(surface, handle if isinstance(handle, str) else None)
        op = args.get("op") or (call.name if call.name in _OP_NAMES else None)
        role = args.get("role") or (aff.role if aff is not None else None)
        out.append(
            Candidate(
                label=label,
                handle=handle if isinstance(handle, str) else None,
                op=op if isinstance(op, str) else None,
                role=role if isinstance(role, str) else None,
                http_method=(
                    str(args["http_method"]) if args.get("http_method") else None
                ),
            )
        )
    return out


# Generic interaction verbs a tool name may itself be (so a bare ``fill``/``submit``
# tool call carries an op signal to the classifier without explicit args).
_OP_NAMES = frozenset(
    {"fill", "read", "open", "select", "expand", "focus",
     "submit", "confirm", "purchase", "pay", "checkout", "delete", "click"}
)


def _proposal_request(
    surface: SurfaceView, recognized: RecognitionResult | None, *, k: int
) -> ModelRequest:
    """The ModelRequest handed to the proposing policy: the surface affordances and
    the recognizer's PRIOR (archetype + handles), asking for ≤K candidate moves as
    tool calls. The recognized handles are surfaced as a hint to bias move ordering;
    the model is free to ignore them (the lookahead/rail still DISPOSES)."""
    affs = [
        {"handle": a.handle, "role": a.role, "label": a.label} for a in surface.affordances
    ]
    hint: dict[str, Any] = {}
    if recognized is not None:
        hint = {
            "archetype": recognized.archetype,
            "confidence": recognized.confidence,
            "suggested_handles": list(recognized.matched_handles),
        }
    sys = (
        "Propose up to K candidate next actions over the current surface as tool "
        "calls. Each call's args carry {label, handle, op?, role?}. You PROPOSE; a "
        "deterministic lookahead over the learned model disposes — do not commit."
    )
    user = {"k": k, "url": surface.url, "title": surface.title,
            "affordances": affs, "prior": hint}
    import json

    return ModelRequest(
        messages=[{"role": "system", "content": sys},
                  {"role": "user", "content": json.dumps(user)}]
    )


# An injected executor: act ONE step in the real world (browser/tool) and return
# the resulting ``SurfaceView``. Offline tests inject a fake returning scripted
# next-surfaces; a real run drives the browser. It is async and may be awaited.
ActionExecutor = Callable[[Candidate, SurfaceView], Awaitable[SurfaceView]]

# An optional REPAIR HOOK — the plain-callable, zu-shadow-free shape of the
# escalate→diagnose→repair seam (§5 mpc parity). It speaks ONLY zu-core currency
# (``ProblemContext`` in, ``Repair`` out), so ``zu-patterns`` never imports
# ``zu-shadow`` (a cycle — §9.9). ``mpc_run`` calls it BEFORE the blind structural
# sibling-replan on the two stuck signals (a TRAP — ``decision.action is None`` —
# and a post-executor ``no_op``). A ``Repair`` whose ``kind`` is ``'human'`` /
# ``'abort'`` STOPS the loop (escalate, no rollback); any other answer falls
# through to the structural rollback. The hook is content-free here: ``mpc_run`` is
# a pure FSM planner with no parsed page, so it hands the hook an empty diagnostic
# ``ContentView`` — a real run supplies the slice. NEVER auto-applies a fill across
# a commit boundary: a COMMITTING decision escalates WITHOUT ever reaching the hook.
RepairHook = Callable[[ProblemContext], Awaitable[Repair]]


@dataclass(frozen=True)
class MpcOutcome:
    """The result of an ``mpc_run`` driver loop.

    ``rollbacks`` counts the MPC-level structural rollbacks performed: on a trap (no
    on-rail non-committing forward branch) the loop reverts to the last checkpoint
    surface and replans a DIFFERENT untried on-rail sibling (ZU-RAIL-8, structural —
    see ``mpc_run``), bounded by ``replan_budget``."""

    reached_goal: bool
    escalated: bool
    steps: tuple[Candidate, ...]
    rationale: str
    surface: SurfaceView
    rollbacks: int = 0


async def _consult_repair(
    repair: RepairHook | None,
    *,
    index: int,
    surface: SurfaceView,
    reason: str,
) -> Repair | None:
    """Ask the optional repair hook about a stuck step (§5 mpc parity).

    Returns the ``Repair`` the hook proposed, or ``None`` when no hook is wired.
    The context speaks ONLY zu-core currency — the content-free action ``surface``,
    the step ``index`` (the resume cursor), the stuck ``reason``, and an EMPTY
    diagnostic ``ContentView`` (``mpc_run`` is a pure FSM planner with no parsed
    page; a real run supplies the slice). A content read never feeds the FSM key, so
    this leaves ``surface_state_id`` content-free."""
    if repair is None:
        return None
    ctx = ProblemContext(
        index=index, surface=surface, view=ContentView(url=surface.url), reason=reason
    )
    return await repair(ctx)


async def mpc_run(
    surface: SurfaceView,
    model: ModelProvider,
    fsm: Fsm,
    executor: ActionExecutor,
    patterns: Sequence[Any] = (),
    *,
    k: int = 3,
    depth: int = 2,
    max_steps: int = 25,
    surface_to_state: SurfaceToState | None = None,
    priors: Sequence[Any] = (),
    min_confidence: float = 0.6,
    dead_edges: frozenset[DeadEdge] = frozenset(),
    replan_budget: int = 0,
    on_rollback: Callable[[str, Candidate], Awaitable[None]] | None = None,
    repair: RepairHook | None = None,
) -> MpcOutcome:
    """The driver loop: ``live_mpc_step`` → execute ONE step via the injected
    ``executor`` → re-plan from the REAL resulting state → repeat.

    Stops when: the goal FSM state is reached (success), a trap/terminal/no-on-rail
    candidate is hit (escalate), or a COMMITTING step is chosen (STOP at the commit
    boundary — escalate, NEVER auto-cross). Reversible/idempotent steps execute
    freely. ``max_steps`` bounds the loop. The executor is the only I/O; everything
    else is the pure decision above, so the whole loop runs offline with a fake
    executor.

    STRUCTURAL ROLLBACK + REPLAN (ZU-RAIL-8, the planner-level hook). When a step
    TRAPS — ``live_mpc_step`` escalates with NO on-rail candidate (``action is None``)
    — and ``replan_budget`` remains, the loop ROLLS BACK to the last checkpoint
    surface (the surface before the trapping move) and re-calls ``live_mpc_step``
    there with ``exclude={already-tried sibling labels}``, so it replans a DIFFERENT
    on-rail sibling instead of escalating immediately. This is a STRUCTURAL rollback
    (revert ``cur`` to the checkpoint surface + exclude the tried label) — NOT the
    event-sourced ``zu_core.rollback_and_replan`` (which needs a real run + event
    log; ``mpc_run`` is a pure offline planner over an ``Fsm``). Consume-once is
    preserved by construction: a COMMITTING decision is the commit boundary and
    escalates WITHOUT a rollback, so only REVERSIBLE siblings are ever re-tried —
    there is no committed side effect to re-run. The replan is bounded by
    ``replan_budget`` (per-run total) AND by the per-surface ``exclude`` set, so a
    model that keeps proposing the same trap cannot loop forever. Default
    ``replan_budget=0`` ⇒ opt-in; the legacy escalate-on-trap behavior is unchanged.

    ``dead_edges`` is forwarded into every ``live_mpc_step`` call (the per-run mask
    of ``(state_id, edge_label)`` pairs proven dead this run — routed around, NEVER
    persisted into ``fsm``). ``on_rollback`` is an optional async hook fired when a
    trap triggers a rollback (for audit / checkpoint-event emission).

    ESCALATE→DIAGNOSE→REPAIR (§5 mpc parity, the zu-shadow-free mirror). ``repair``
    is an OPTIONAL plain async callback of the ``(ProblemContext) -> Repair`` shape
    (zu-core currency only — NO zu-shadow import, §9.9). It is consulted on the TWO
    stuck signals BEFORE the blind structural sibling-replan:
      * a TRAP — ``live_mpc_step`` escalated with NO on-rail candidate
        (``decision.action is None``), reason ``'unresolved'``; and
      * a ``no_op`` — a chosen reversible step that EXECUTED but changed nothing
        (``to_state(prev) == to_state(new)`` after the injected executor), reason
        ``'no_op'``.
    On either, the loop asks ``repair`` what to do. A ``Repair`` whose ``kind`` is
    ``'human'`` / ``'abort'`` STOPS the loop (escalate, no rollback); any other
    answer (e.g. ``'fill'``) falls through to the existing structural rollback. The
    repair consultation is BOUNDED by ``replan_budget`` (it shares the same budget
    as the structural replan, so a model that keeps stalling cannot loop) and uses
    ``on_rollback`` as the audit hook. CONSUME-ONCE is preserved exactly as before:
    a COMMITTING decision escalates WITHOUT a rollback and WITHOUT consulting the
    hook, so a repair NEVER crosses a commit boundary — only reversible siblings are
    ever re-tried. ``surface_state_id`` stays content-free: the hook reads the
    action view + reason, never the surface state id (a content read never feeds the
    FSM key). Default ``repair=None`` ⇒ legacy behavior unchanged."""
    to_state = surface_to_state or _surface_state
    taken: list[Candidate] = []
    cur = surface
    rollbacks = 0
    # The last surface that produced an on-rail step (the checkpoint to roll back
    # to) and, per surface-state, the sibling labels already TRIED there. On a trap
    # the loop reverts ``cur`` to the checkpoint surface and excludes the tried
    # labels so the replan picks a DIFFERENT on-rail sibling.
    checkpoint = cur
    tried: dict[str, set[str]] = {}
    for _ in range(max_steps):
        if to_state(cur) in fsm.accepting:
            return MpcOutcome(
                reached_goal=True, escalated=False, steps=tuple(taken),
                rationale="reached goal state", surface=cur, rollbacks=rollbacks,
            )
        here = to_state(cur)
        decision = await live_mpc_step(
            cur, model, fsm, patterns, k=k, depth=depth,
            surface_to_state=to_state, priors=priors, min_confidence=min_confidence,
            dead_edges=dead_edges, exclude=frozenset(tried.get(here, set())),
        )
        if decision.escalate or decision.action is None:
            # A TRAP (no on-rail candidate, ``action is None``) MAY escalate→repair
            # then roll back to the last checkpoint surface and replan a DIFFERENT
            # sibling — but a COMMIT-BOUNDARY escalation (a candidate WAS chosen but
            # is COMMITTING) NEVER does: stopping at the commit boundary is the whole
            # point, and a re-try could re-cross a committed side effect. Only a
            # genuine trap repairs/rolls back, so only REVERSIBLE siblings are ever
            # re-tried — consume-once preserved.
            if decision.action is None and replan_budget > 0:
                # (1) escalate→diagnose→repair, BEFORE the blind structural replan
                # (§5 mpc parity). The hook reads the content-free action view +
                # reason ('unresolved'); a 'human'/'abort' answer STOPS the loop
                # (escalate, no rollback) — a repair never crosses a commit boundary.
                rep = await _consult_repair(
                    repair, index=len(taken), surface=cur, reason="unresolved"
                )
                if rep is not None and rep.kind in ("human", "abort"):
                    return MpcOutcome(
                        reached_goal=False, escalated=True, steps=tuple(taken),
                        rationale=f"repair → {rep.kind}: {rep.reason}".rstrip(": "),
                        surface=cur, rollbacks=rollbacks,
                    )
                # (2) structural rollback: exclude the off-rail labels the model just
                # proposed at ``checkpoint`` (so the replan picks a sibling), revert
                # ``cur`` to the checkpoint surface, and give the model another turn.
                cp_state = to_state(checkpoint)
                proposed = {c.label for c, _ in decision.scored}
                tried.setdefault(cp_state, set()).update(proposed)
                if on_rollback is not None and decision.scored:
                    await on_rollback("trap", decision.scored[0][0])
                cur = checkpoint
                replan_budget -= 1
                rollbacks += 1
                continue
            return MpcOutcome(
                reached_goal=False, escalated=True, steps=tuple(taken),
                rationale=decision.rationale, surface=cur, rollbacks=rollbacks,
            )
        # execute exactly ONE reversible step via the injected executor, then
        # re-plan from the REAL resulting surface. This surface is now the last
        # known-good checkpoint, and the chosen label is tried here.
        tried.setdefault(here, set()).add(decision.action.label)
        prev = cur
        checkpoint = cur
        taken.append(decision.action)
        cur = await executor(decision.action, cur)
        # POST-EXECUTOR NO-OP CHECK (§5 mpc parity): the reversible step fired but
        # changed nothing — ``to_state`` is content-free (url+title+handle digest),
        # so an error-text variant alone does NOT register as a no_op (the FSM key
        # is stable across error text). Route the no_op through the SAME repair hook
        # BEFORE the structural sibling-replan, bounded by ``replan_budget``.
        if to_state(prev) == to_state(cur) and replan_budget > 0:
            rep = await _consult_repair(
                repair, index=len(taken) - 1, surface=cur, reason="no_op"
            )
            if rep is not None and rep.kind in ("human", "abort"):
                return MpcOutcome(
                    reached_goal=False, escalated=True, steps=tuple(taken),
                    rationale=f"repair → {rep.kind}: {rep.reason}".rstrip(": "),
                    surface=cur, rollbacks=rollbacks,
                )
            # structural rollback: the no-op label is already in ``tried[here]``
            # (recorded above), so the replan from ``checkpoint`` picks a DIFFERENT
            # on-rail sibling. Revert ``cur`` to the checkpoint and consume budget.
            if on_rollback is not None:
                await on_rollback("no_op", decision.action)
            cur = checkpoint
            replan_budget -= 1
            rollbacks += 1
            continue
    return MpcOutcome(
        reached_goal=to_state(cur) in fsm.accepting, escalated=False,
        steps=tuple(taken), rationale="max_steps reached", surface=cur,
        rollbacks=rollbacks,
    )


# --- (D) the transition model FROM SHADOW recordings (Part B) -------------
#
# ``fsm_from_events`` folds the EVENT LOG into an ``Fsm``. ``fsm_from_shadow`` does
# the same from a Shadow recording, so a recording and the event log feed the SAME
# search transition model. The shapes are aligned (both produce a ``reachability.
# Fsm``), so the two sources MERGE — accumulating recordings GROWS the learned
# graph (the apprenticeship premise).
#
# DEPENDENCY DIRECTION: zu-shadow depends on zu-core AND zu-cli. Importing zu-shadow
# from zu-patterns risks a package cycle and violates the "dependency-light" rule,
# so ``fsm_from_shadow`` takes PLAIN inputs — either the already-emitted induced
# ``Fsm`` (the synthesizer's ``SynthesisResult.fsm``) OR the list of shadow events
# (``data.shadow.user.*``) — and NEVER imports zu-shadow. zu-patterns still depends
# only on zu-core.


def _shadow_state_id(seq: int) -> str:
    return f"shadow_s{seq}"


def _shadow_action_label(e: Any) -> str:
    """The edge label for one ``data.shadow.user.*`` event — verb[:target], the
    same human-readable shape the synthesizer's ``_action_label`` produces."""
    t = getattr(e, "type", "")
    p = _payload(e)
    if t == ev.SHADOW_USER_NAVIGATE:
        return "navigate"
    verb = "click" if t == ev.SHADOW_USER_CLICK else "type"
    target = p.get("target") or {}
    name = ""
    if isinstance(target, dict):
        name = target.get("name") or target.get("label") or target.get("role") or ""
    return f"{verb}:{name}" if name else verb


def fsm_from_shadow_events(
    events: Sequence[Any],
    *,
    initial: str = "shadow_start",
    goal: str = "shadow_goal",
) -> Fsm:
    """Fold a Shadow recording's ``data.shadow.user.*`` action sequence into an
    induced ``Fsm`` (pure) — the SAME shape ``fsm_from_events`` and the synthesizer's
    ``induce_fsm`` produce, so the search transition model is source-agnostic.

    One state per recorded action, an edge per consecutive pair labelled by the
    action, ``initial`` → … → ``goal`` (a ``done`` edge into the accepting goal).
    Takes plain events (no zu-shadow import)."""
    actions = [
        e for e in events
        if getattr(e, "type", "") in (
            ev.SHADOW_USER_CLICK, ev.SHADOW_USER_TYPE, ev.SHADOW_USER_NAVIGATE
        )
    ]
    states = [initial]
    edges: list[FsmEdge] = []
    prev = initial
    for i, e in enumerate(actions):
        s = _shadow_state_id(i + 1)
        states.append(s)
        edges.append(FsmEdge(src=prev, dst=s, label=_shadow_action_label(e)))
        prev = s
    states.append(goal)
    edges.append(FsmEdge(src=prev, dst=goal, label="done"))
    return Fsm(
        states=frozenset(states),
        initial=initial,
        accepting=frozenset({goal}),
        edges=tuple(edges),
    )


def merge_transition_models(*fsms: Fsm) -> Fsm:
    """Merge induced ``Fsm``s into ONE learned transition model — the union of
    states and edges (de-duplicated), so accumulating recordings GROWS the graph.

    The first FSM's ``initial`` is kept as the merged initial; the accepting sets
    union (any source's goal is a goal). Pure set/tuple algebra — no new machinery,
    just graph union over ``reachability.Fsm``. This is what lets a Shadow recording
    and the event log feed the same search, and a second recording extend the
    first."""
    if not fsms:
        return Fsm(states=frozenset(), initial="", accepting=frozenset(), edges=())
    states: set[str] = set()
    accepting: set[str] = set()
    seen_edges: set[tuple[str, str, str]] = set()
    edges: list[FsmEdge] = []
    for f in fsms:
        states |= f.states
        accepting |= f.accepting
        for e in f.edges:
            key = (e.src, e.dst, e.label)
            if key not in seen_edges:
                seen_edges.add(key)
                edges.append(e)
    return Fsm(
        states=frozenset(states),
        initial=fsms[0].initial,
        accepting=frozenset(accepting),
        edges=tuple(edges),
    )


def fsm_from_shadow(
    source: Any,
    *,
    base: Fsm | None = None,
    initial: str = "shadow_start",
    goal: str = "shadow_goal",
) -> Fsm:
    """Build/extend the empirical transition model from a Shadow recording (Part B).

    ``source`` is taken as PLAIN input — no zu-shadow import (dependency-light):

      * an already-emitted ``reachability.Fsm`` (the synthesizer's induced FSM,
        ``SynthesisResult.fsm``) — consumed directly; or
      * a sequence of shadow events (``data.shadow.user.*``) — folded via
        ``fsm_from_shadow_events`` into the SAME shape; or
      * an object exposing ``.events`` / ``.shadow_events()`` (a RecordedSession-
        shaped duck) — its events are folded.

    When ``base`` is given, the new model is MERGED into it (``merge_transition_
    models``), so a SECOND recording GROWS the learned graph — the apprenticeship
    premise. The result feeds the SAME ``plan`` / ``live_mpc_step`` search."""
    if isinstance(source, Fsm):
        induced = source
    else:
        events = _shadow_events_of(source)
        induced = fsm_from_shadow_events(events, initial=initial, goal=goal)
    if base is not None:
        return merge_transition_models(base, induced)
    return induced


def _shadow_events_of(source: Any) -> Sequence[Any]:
    """Extract the shadow events from a plain input: a bare sequence, or a
    RecordedSession-shaped object exposing ``shadow_events()`` / ``events``."""
    shadow_events = getattr(source, "shadow_events", None)
    if callable(shadow_events):
        return list(shadow_events())
    events = getattr(source, "events", None)
    if events is not None:
        return list(events)
    if isinstance(source, Sequence):
        return source
    raise TypeError(
        "fsm_from_shadow source must be an Fsm, a sequence of shadow events, or a "
        "RecordedSession-shaped object (with .events / .shadow_events())"
    )
