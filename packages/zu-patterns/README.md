# zu-patterns — the policy-prior / move-ordering layer (§5)

A UI is a state space; the **Action Surface** is the move generator (affordances
= legal moves). This package is the **policy prior + guided search** layer over
that surface — the *AlphaZero* shape, not Deep Blue. It does **not** brute-force a
live space (visiting a node might charge a card). It proposes the promising
interaction *without* exploring, and the rail (§1) verifies the prediction.

## The new port — `Pattern` (group `zu.patterns`)

The `Pattern` Protocol lives in **zu-core** (`zu_core.ports.Pattern`), like the
other ports. A pattern is **read-only**: it `recognize`s a situation over a core
`SurfaceView` (`zu_core.surface`) and emits `success_invariants` /
`failure_invariants` (declarative `zu_core.invariants.Invariant`s the rail
verifies). It **never** calls a tool and **never** decides the task action — that
is the policy's job. A recognized pattern is a **prior to be confirmed by
observation, never ground truth** (ZU-RAIL-9): its success criteria compile (via
`compile_spec`) to Monitors, and a behaviour mismatch fires a detector.

The boundary that makes this clean: `recognize` takes the **core** `SurfaceView`,
never zu-tools' `Surface`. zu-tools projects its `Surface` onto `SurfaceView`
through a one-way adapter (`zu_tools.surface_adapter.to_surface_view`), so
zu-patterns depends only on zu-core.

## The pieces

- `recognizer.py` — a pure pass over a `SurfaceView`: classify → archetype +
  confidence. Low confidence ⇒ **no hint** (fall through to the model).
- `reversibility.py` — a principled, default-to-committing classifier of an
  action as **reversible** (read-only/idempotent, safe to explore live) vs
  **committing** (side-effecting — the live-search/rail commit boundary). No
  site-specific keyword blocklist: HTTP-method/idempotency, affordance semantics,
  an extensible prior set, default-to-committing on uncertainty.
- `rail.py` — the success/failure → `Invariant` helpers shared by the patterns.
- `search.py` — an offline best-first planner **over the Phase-1
  `zu_core.reachability.Fsm`** with the recognizer as the move-ordering prior,
  plus an event-log → `Fsm` transition-model builder. Pure, offline, $0. The
  live guided-MPC loop and the Shadow-sourced transition model are **deferred
  seams** (stubbed/documented).

## The 8 starter archetypes

`cookie_banner`, `login_form`, `search_box`, `modal_dialog`, `paginated_list`,
`sortable_table`, `autocomplete`, `cart_checkout` — the last is the canonical
**irreversible-boundary** pattern (its place-order/pay step is classified
COMMITTING; the script stops before it).

All recognition is **deterministic** structural matching over roles/labels/states
(no model), so every pattern is tested at $0 with hand-built `SurfaceView`s. A
small-model recognizer is a future plugin behind the same Protocol.
