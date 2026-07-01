"""autocomplete — a textbox/combobox (expanded / controls a listbox of options).

Script: fill partial, pick option. Typing + picking is REVERSIBLE (it fills a
field). Success: the option fills the field.
"""

from __future__ import annotations

from zu_core.invariants import Invariant
from zu_core.ports import PatternStep, RecognitionResult
from zu_core.surface import SurfaceView

from . import _match as m
from .confidence import STRONG, TENTATIVE
from .rail import surface_shows

_EXPAND_STATES = ("expanded", "haspopup", "autocomplete")


class Autocomplete:
    name = "autocomplete"
    archetype = "autocomplete"

    def recognize(self, surface: SurfaceView) -> RecognitionResult | None:
        box = next(
            (
                a
                for a in surface.affordances
                if a.role.lower() in {"combobox", "textbox", "searchbox"}
                and m.has_state(a, *_EXPAND_STATES)
            ),
            None,
        )
        if box is None:
            return None
        options = m.of_role(surface, "option")
        # An expanded combobox with options present is the strong case.
        confidence = STRONG if options else TENTATIVE
        script = [PatternStep(op="fill", role=box.role, label_hint=m.norm(box.label), note="type")]
        handles = [box.handle]
        if options:
            opt = options[0]
            script.append(
                PatternStep(op="select", role="option", label_hint=m.norm(opt.label), note="pick")
            )
            handles.append(opt.handle)
        return RecognitionResult(
            archetype=self.archetype,
            confidence=confidence,
            matched_handles=tuple(handles),
            script=tuple(script),
            detail="autocomplete",
        )

    def success_invariants(self, result: RecognitionResult) -> list[Invariant]:
        handle = result.matched_handles[0] if result.matched_handles else None
        # Done = the field EVENTUALLY present (filled) by the deadline.
        return [surface_shows(self.archetype, "option_filled", handle=handle, liveness=True)]

    def failure_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Failure CONTEXT = a "no results" empty-state appears. Safety shape:
        # THROUGHOUT NOT contains(no results) — fires the instant it lands.
        return [surface_shows(self.archetype, "no_options", label="no results", negate=True)]
