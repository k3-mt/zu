"""modal_dialog — an alertdialog/dialog trapping focus with a close/confirm.

Reversible when the proposed step is a close/dismiss; COMMITTING when it is a
confirm/proceed (a confirm may commit an action). Success: the dialog is gone.
"""

from __future__ import annotations

from zu_core.invariants import Invariant
from zu_core.ports import PatternStep, RecognitionResult
from zu_core.surface import SurfaceView

from . import _match as m
from .confidence import GOOD, LOW
from .rail import surface_shows

_DIALOG_CONTEXT = ("dialog", "modal", "are you sure", "please confirm")
# Dialog failure vocabulary — an any-of set (#46), not the single literal "error".
_ERROR_TOKENS = ("error", "failed", "something went wrong", "try again")


class ModalDialog:
    name = "modal_dialog"
    archetype = "modal_dialog"

    def recognize(self, surface: SurfaceView) -> RecognitionResult | None:
        # A dialog surfaces as a close button and/or a confirm button, with
        # dialog-ish context. (Cookie banners are their own pattern and rank
        # higher via consent vocabulary; this is the generic modal.)
        close = m.first(surface, roles=("button",), tokens=m.CLOSE_TOKENS)
        confirm = m.first(surface, roles=("button",), tokens=m.CONFIRM_TOKENS)
        if close is None and confirm is None:
            return None
        dialogish = m.context_has(surface, _DIALOG_CONTEXT)
        # Avoid stealing the cookie-banner case: if it reads as consent, defer.
        if m.context_has(surface, ("cookie", "consent")):
            return None
        if not dialogish and close is None:
            return None
        chosen = close or confirm
        assert chosen is not None
        # Prefer the reversible close step in the proposed script.
        op = "click" if chosen is close else "confirm"
        confidence = GOOD if dialogish else LOW
        return RecognitionResult(
            archetype=self.archetype,
            confidence=confidence,
            matched_handles=(chosen.handle,),
            script=(
                PatternStep(op=op, role="button", label_hint=m.norm(chosen.label), note="dismiss"),
            ),
            detail="modal dialog",
        )

    def success_invariants(self, result: RecognitionResult) -> list[Invariant]:
        handle = result.matched_handles[0] if result.matched_handles else None
        # Done = the dialog is EVENTUALLY gone (liveness-of-ABSENCE: the dialog is
        # present pre-dismiss, so it must not fire until the deadline; a dialog that
        # never closes by the deadline VIOLATES this liveness — the "persists"
        # failure, captured without a redundant positive must-contain-THROUGHOUT).
        return [surface_shows(self.archetype, "dismissed", handle=handle, negate=True, liveness=True)]

    def failure_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Failure CONTEXT = an error appears inside/after the dialog. Safety shape:
        # THROUGHOUT NOT contains(error) — fires the instant it lands. (The
        # "persists" mode is the success liveness deadline-violation, not duplicated.)
        return [
            surface_shows(self.archetype, "dialog_error", labels=_ERROR_TOKENS, negate=True)
        ]
