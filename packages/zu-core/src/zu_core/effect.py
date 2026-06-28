"""Action-effect verification — did an act actually change the surface, or was it a
SILENT NO-OP? (a styled swatch that didn't select, a "type" that landed on a button).

This is the GENERALISATION, upstream into zu-core, of the stand-in that has been living
DOWNSTREAM in conduit (``conduit_api.effect.verify_effect``). Putting it here — a pure
function over the modality-agnostic :class:`zu_core.surface.SurfaceView` — means every
consumer of the runtime inherits the same content-free effect oracle, not just conduit.

Content-free (§9): it reads only the SHAPE of the surface before and after — the acted
control's states/value, whether the set of affordance labels changed, and the overall
:meth:`SurfaceView.fingerprint`. It NEVER reads page meaning or secrets. An action that
demonstrably changed shape returns ``None``; one that left the surface identical returns
``"silent-no-op"``.

The loop calls this opportunistically — when a handle-click is bracketed by two captured
surfaces — and records the verdict as a ``data.effect.verified`` event, so a silent no-op
is on the auditable log and (when it fires) surfaced back to the policy as a non-fatal
signal it can react to. It is deterministic and replayable: a pure function of the two
frozen surfaces, no I/O, no clock.
"""

from __future__ import annotations

from .surface import SurfaceAffordance, SurfaceView


def _ident(a: SurfaceAffordance) -> tuple[str, str]:
    """A control's render-STABLE identity: role + label, lowercased. Handles are
    per-render (a click can re-render and renumber every handle), so identity — not the
    handle — is what recognises the SAME control before and after an action."""
    return (a.role.strip().lower(), a.label.strip().lower())


def verify_effect(before: SurfaceView, after: SurfaceView, acted_handle: str) -> str | None:
    """Return ``"silent-no-op"`` if acting on ``acted_handle`` demonstrably changed
    nothing, else ``None``.

    Biased toward NOT crying no-op (a no-op verdict requires ALL of: no control's
    state/value changed, no label-set delta, and an unchanged fingerprint) — so a real
    change is never mistaken for a dead action and a good run is never stalled. Four
    independent shape signals, any one of which means "something happened":
    """
    b_by_handle = {a.handle: a for a in before.affordances}
    a_by_handle = {a.handle: a for a in after.affordances}

    # 1) The acted control's own state/value changed (a radio became selected, a field
    #    took a value). A click often RE-RENDERS and renumbers handles, so the acted
    #    handle may be gone from `after` — re-find the same control by its (role, label)
    #    IDENTITY before concluding nothing changed.
    bh = b_by_handle.get(acted_handle)
    if bh is not None:
        ah = a_by_handle.get(acted_handle)
        if ah is None:
            ah = next((x for x in after.affordances if _ident(x) == _ident(bh)), None)
        if ah is not None and (tuple(bh.states) != tuple(ah.states) or bh.value != ah.value):
            return None

    # 2) ANY control's selection state changed, keyed by IDENTITY (the acted swatch
    #    became selected, or a sibling deselected) — robust to handle churn. This catches
    #    a styled colour/size swatch whose only visible change is its own
    #    aria-checked/pressed/selected flipping while every label stays present.
    a_states = {_ident(x): tuple(x.states) for x in after.affordances}
    for x in before.affordances:
        key = _ident(x)
        if key in a_states and a_states[key] != tuple(x.states):
            return None

    # 3) The set of affordance labels changed at all — something appeared (a cart drawer,
    #    a "Remove"/"View cart", a next step) OR disappeared (navigated away).
    b_labels = {x.label for x in before.affordances}
    a_labels = {x.label for x in after.affordances}
    if a_labels != b_labels:
        return None

    # 4) The overall surface shape changed (navigation, a re-render with new structure,
    #    or a value change on any control) — the fingerprint folds role+label+value+states
    #    of every affordance, so it moves on any of those while ignoring handle renumbering.
    if before.fingerprint() != after.fingerprint():
        return None

    return "silent-no-op"
