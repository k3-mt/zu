"""login_form — a username/email textbox + a password textbox + a submit button.

Script (a PROPOSAL, never auto-run): fill user, fill password, click submit.
Submit is COMMITTING (a form POST). Success: a post-submit surface shows an
account/logout/profile affordance. Failure: an error alert/status appears.
"""

from __future__ import annotations

from zu_core import events as ev
from zu_core.invariants import Invariant
from zu_core.ports import PatternStep, RecognitionResult
from zu_core.surface import SurfaceView

from . import _match as m
from .rail import surface_shows

_ACCOUNT_TOKENS = ("logout", "log out", "sign out", "account", "profile", "my account")
_ERROR_TOKENS = ("invalid", "incorrect", "error", "wrong password", "failed", "try again")


class LoginForm:
    name = "login_form"
    archetype = "login_form"

    def recognize(self, surface: SurfaceView) -> RecognitionResult | None:
        user = m.first(surface, roles=("textbox", "searchbox"), tokens=m.USER_TOKENS)
        # A password field is a textbox carrying a 'password' state, or labelled so.
        pw = next(
            (
                a
                for a in m.of_role(surface, "textbox")
                if m.has_state(a, "password") or m.label_has(a, m.PASSWORD_TOKENS)
            ),
            None,
        )
        submit = m.first(surface, roles=("button",), tokens=m.SUBMIT_TOKENS)
        if user is None or pw is None:
            return None
        # Confidence rises with a submit button present and a password state.
        confidence = 0.7
        if submit is not None:
            confidence += 0.15
        if m.has_state(pw, "password"):
            confidence += 0.1
        confidence = min(1.0, confidence)
        handles = tuple(h for h in (user.handle, pw.handle, submit.handle if submit else None) if h)
        script = [
            PatternStep(op="fill", role="textbox", label_hint=m.norm(user.label), note="username"),
            PatternStep(op="fill", role="textbox", label_hint=m.norm(pw.label), note="password"),
        ]
        if submit is not None:
            script.append(
                PatternStep(
                    op="submit", role="button", label_hint=m.norm(submit.label), note="commit"
                )
            )
        return RecognitionResult(
            archetype=self.archetype,
            confidence=confidence,
            matched_handles=handles,
            script=tuple(script),
            detail="login form",
        )

    def success_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Done = a post-submit surface EVENTUALLY shows an account/logout
        # affordance (a liveness-by-deadline postcondition: absent until the submit
        # completes, so it must NOT fire on the pre-submit login surface).
        return [
            surface_shows(self.archetype, "reached_account", label=tok, liveness=True)
            for tok in ("Logout", "Sign out", "Account")
        ][:1]

    def failure_invariants(self, result: RecognitionResult) -> list[Invariant]:
        # Failure CONTEXT = an error alert/status appeared. Correct safety shape:
        # "THROUGHOUT: NOT contains(error)" — the Monitor fires the instant an
        # error context lands; the pre-interaction surface (no error) satisfies it.
        return [
            surface_shows(
                self.archetype,
                "error_alert",
                label="error",
                event_type=ev.SURFACE_CAPTURED,
                negate=True,
            )
        ]
