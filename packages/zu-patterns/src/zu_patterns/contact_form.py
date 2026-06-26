"""contact_form — a fillable form region that COMMITS contact/shipping detail.

Fires when a surface shows a fillable form whose slots read like a contact or
shipping form (postcode/city/address line/full name/phone), OR carries an OTP /
one-time-code field, OR simply offers >=3 distinct fillable fields. The submit
step is COMMITTING (a form POST that hands over real personal data) — the live
search and rail commit boundary. A bare email + <=2 fields with no shipping/OTP
vocabulary is NOT this pattern (that is newsletter_signup's territory).

Script (a PROPOSAL, never auto-run): fill each fillable slot, then submit.
Success: a post-submit confirmation surface. Failure: a validation/error alert.
"""

from __future__ import annotations

from zu_core.invariants import Invariant
from zu_core.ports import PatternStep, RecognitionResult
from zu_core.surface import SurfaceAffordance, SurfaceView

from . import _match as m
from .rail import surface_shows
from .reversibility import ActionPrior, Commitment

# Roles a human types into — the fillable surface of a form region.
_FILLABLE_ROLES = ("textbox", "searchbox", "combobox")
_CONFIRM_CONTEXT = ("thank you", "message sent", "we'll be in touch", "submission received")
_ERROR_TOKENS = ("required", "invalid", "error", "please enter", "try again")


def _is_password(aff: SurfaceAffordance) -> bool:
    """A password field — excluded from the fillable count so a login surface
    (login_form's 0.95 territory) never trips the >=3-field fallback."""
    return m.has_state(aff, "password") or m.label_has(aff, m.PASSWORD_TOKENS)


def _is_otp(aff: SurfaceAffordance) -> bool:
    """An OTP/one-time-code field — by label token OR a free-form state."""
    return m.label_has(aff, m.OTP_TOKENS) or m.has_state(aff, "otp", "one-time-code")


class ContactForm:
    name = "contact_form"
    archetype = "contact_form"

    def recognize(self, surface: SurfaceView) -> RecognitionResult | None:
        # The fillable region, MINUS password fields (login's territory). An OTP
        # field is kept even though "passcode" reads as a password token — a
        # one-time code is a contact-form tell, not a login secret.
        fillable = [
            a
            for a in m.of_role(surface, *_FILLABLE_ROLES)
            if _is_otp(a) or not _is_password(a)
        ]
        if not fillable:
            return None
        shipping = [a for a in fillable if m.label_has(a, m.SHIPPING_TOKENS)]
        otp = [a for a in fillable if _is_otp(a)]
        # A contact/shipping slot or an OTP field is a strong, worded tell.
        if shipping or otp:
            confidence = 0.85
        # The bare fallback: >=3 distinct fillable fields with no special vocab.
        elif len(fillable) >= 3:
            confidence = 0.62
        else:
            return None
        # The proposed script fills each slot, then submits — submit is COMMITTING.
        script: list[PatternStep] = [
            PatternStep(op="fill", role=a.role, label_hint=m.norm(a.label), note="contact field")
            for a in fillable
        ]
        submit = m.first(surface, roles=("button",), tokens=m.SUBMIT_TOKENS)
        handles = [a.handle for a in fillable]
        if submit is not None:
            script.append(
                PatternStep(op="submit", role="button", label_hint=m.norm(submit.label), note="commit")
            )
            handles.append(submit.handle)
        return RecognitionResult(
            archetype=self.archetype,
            confidence=confidence,
            matched_handles=tuple(handles),
            script=tuple(script),
            detail="contact/shipping form",
        )

    def success_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Done = a post-submit confirmation surface EVENTUALLY appears (liveness-by-
        # deadline: absent until the submit completes, so it must NOT fire pre-submit).
        return [surface_shows(self.archetype, "submitted", label="thank you", liveness=True)]

    def failure_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Failure CONTEXT = a validation/error alert appears. Safety shape:
        # THROUGHOUT NOT contains(error) — fires the instant the error lands.
        return [surface_shows(self.archetype, "form_error", label="error", negate=True)]

    # The reversibility prior this pattern CONTRIBUTES: its submit step is
    # COMMITTING (a form POST that hands over real contact/shipping data). A
    # planner/classifier passes this into ``classify_action`` so the boundary is
    # declared by the pattern, not hardcoded into the core classifier.
    @staticmethod
    def commit_prior() -> ActionPrior:
        def _is_submit(facts: dict) -> bool:
            op = str(facts.get("op", "")).lower()
            return op in {"submit", "confirm"}

        return ActionPrior(
            name="contact_form.submit",
            matcher=_is_submit,
            commitment=Commitment.COMMITTING,
            weight=2.0,
        )
