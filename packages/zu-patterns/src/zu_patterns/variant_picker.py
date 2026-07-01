"""variant_picker — a group of selectable options (swatches / variants / radio-like
controls), recognized STRUCTURALLY, never by product vocabulary.

A product page's colour/size picker, a radio-group, a set of tabs — anything that
presents a GROUP of mutually-selectable options — is the archetype here. It is
recognized by SHAPE: two or more affordances in a selectable role (radio / option /
tab / swatch), at least one of which carries (or can carry) a selected-style state.
No label vocabulary is consulted: "Red"/"Blue"/"XL" are product names, and this
pattern must fire on ANY such group without knowing them (§9, generic-not-hardcoded).

Selecting an option is REVERSIBLE (picking a different swatch just re-selects). The
success criterion is the content-free "a control became selected" invariant (#39):
the acted option EVENTUALLY reaches a selected state on a captured surface — keyed
on the option's own state transition (reusing the SurfaceView ``states`` an option
carries and the #38 state-delta primitives), NOT on any page text. It fires exactly
when a previously-unselected option becomes selected — the silent-no-op ("I clicked
the swatch, the click was accepted, but nothing got selected") is caught as a
liveness VIOLATION at the deadline.
"""

from __future__ import annotations

from zu_core.invariants import Invariant
from zu_core.ports import PatternStep, RecognitionResult
from zu_core.surface import SurfaceAffordance, SurfaceView

from . import _match as m
from .confidence import MODERATE, STRONG, WEAK
from .rail import surface_shows


def _is_selectable(aff: SurfaceAffordance) -> bool:
    """A structurally-selectable option: a selectable role, OR any affordance that
    already carries a selected-style state (a styled ``<div role="button">`` swatch
    whose only tell is its aria-selected). Content-free — role/state, never label."""
    if aff.role.lower() in {r.lower() for r in m.SELECTABLE_ROLES}:
        return True
    return m.has_state(aff, *m.SELECTED_STATES)


class VariantPicker:
    name = "variant_picker"
    archetype = "variant_picker"

    def recognize(self, surface: SurfaceView) -> RecognitionResult | None:
        options = [a for a in surface.affordances if _is_selectable(a)]
        # A PICKER is a GROUP: a lone selectable control is a checkbox/toggle, not a
        # variant picker. Require >=2 options sharing the selectable shape.
        if len(options) < 2:
            return None
        # At least one option must be able to express selection — either one is
        # already selected, or they are in a role whose selection is the whole point
        # (radio/option/tab). A flat list of plain links is NOT a picker.
        any_selected = any(m.has_state(a, *m.SELECTED_STATES) for a in options)
        role_group = any(
            a.role.lower() in {r.lower() for r in m.SELECTABLE_ROLES} for a in options
        )
        if not (any_selected or role_group):
            return None
        # Confidence: a role-group WITH a visible selected state is a strong, clearly
        # a picker; a role-group alone (nothing selected yet) is a solid match; a
        # bare state-only group (styled swatches, no semantic role) is weaker.
        if role_group and any_selected:
            confidence = STRONG
        elif role_group:
            confidence = MODERATE
        else:
            confidence = WEAK
        # Propose selecting the FIRST not-yet-selected option (a reversible pick).
        target = next(
            (a for a in options if not m.has_state(a, *m.SELECTED_STATES)), options[0]
        )
        return RecognitionResult(
            archetype=self.archetype,
            confidence=confidence,
            matched_handles=tuple(a.handle for a in options),
            script=(
                PatternStep(
                    op="select",
                    role=target.role,
                    label_hint=m.norm(target.label),
                    note="pick variant",
                ),
            ),
            detail="variant / swatch picker (selectable group)",
            # Declared outcome: a selection is made — a navigational, on-path step of
            # configuring a product/choice, content-free by construction.
            outcome=("select", "variant", "option", "choose"),
        )

    def success_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Done = the ACTED control EVENTUALLY reaches a selected state (#39). This is
        # content-free: it folds the option's own ``states`` on a captured surface —
        # a previously-unselected option becoming selected — NOT any page text. The
        # acted handle is the first matched option (the script's target). A picker
        # click that is accepted but selects NOTHING never satisfies this → the
        # silent no-op VIOLATES the liveness at the deadline.
        handle = result.matched_handles[0] if result.matched_handles else None
        return [
            surface_shows(
                self.archetype,
                "became_selected",
                states=m.SELECTED_STATES,
                handle=handle,
                liveness=True,
            )
        ]

    def failure_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Failure CONTEXT = an out-of-stock / unavailable variant error appears.
        # Safety shape: THROUGHOUT NOT contains(<any error variant>) — fires the
        # instant such a surface lands; the pre-interaction picker satisfies it.
        return [
            surface_shows(
                self.archetype,
                "variant_error",
                labels=("out of stock", "unavailable", "sold out", "error"),
                negate=True,
            )
        ]
