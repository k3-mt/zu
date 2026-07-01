"""Issue #57 — short/symbol tokens match on WORD BOUNDARIES, not raw substring.

A bare "x"/"go"/"ok"/"×" must match a whole-token label ("x" close button, "Go",
"OK") but NOT be a substring of an unrelated label ("Relax", "Google", "Lookout",
"Export"). These assertions FAIL against the old pure-substring ``label_has``.

Pure/offline ($0): direct unit calls over hand-built affordances + a recognizer
check that the wrong affordance is no longer selected as a submit/close target.
"""

from __future__ import annotations

from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_patterns import _match as m
from zu_patterns.cookie_banner import CookieBanner
from zu_patterns.login_form import LoginForm
from zu_patterns.modal_dialog import ModalDialog


def _aff(label: str, role: str = "button", **kw: object) -> SurfaceAffordance:
    return SurfaceAffordance(handle="h", role=role, label=label, **kw)  # type: ignore[arg-type]


def test_close_tokens_word_boundary() -> None:
    for decoy in ("Explore", "Next", "Fax", "Export", "Relax"):
        assert not m.label_has(_aff(decoy), m.CLOSE_TOKENS), decoy
    for hit in ("Close", "Dismiss", "x", "×", "X"):
        assert m.label_has(_aff(hit), m.CLOSE_TOKENS), hit


def test_submit_tokens_word_boundary() -> None:
    for decoy in ("Logout", "Category", "Google"):
        assert not m.label_has(_aff(decoy), m.SUBMIT_TOKENS), decoy
    for hit in ("Go", "Log in", "Login"):
        assert m.label_has(_aff(hit), m.SUBMIT_TOKENS), hit


def test_accept_and_confirm_tokens_word_boundary() -> None:
    for decoy in ("Bookmark", "Lookup", "Lookout"):
        assert not m.label_has(_aff(decoy), m.ACCEPT_TOKENS), decoy
        assert not m.label_has(_aff(decoy), m.CONFIRM_TOKENS), decoy
    for hit in ("OK", "Ok", "OK, got it"):
        assert m.label_has(_aff(hit), m.ACCEPT_TOKENS), hit
    for hit in ("OK", "Ok"):
        assert m.label_has(_aff(hit), m.CONFIRM_TOKENS), hit


def test_login_form_does_not_select_logout_as_submit() -> None:
    """A "Logout" button matches the old bare "go" substring and could be put into
    the login script's submit step; word-boundary matching excludes it."""
    surface = SurfaceView(
        affordances=(
            SurfaceAffordance(handle="a1", role="textbox", label="Email"),
            SurfaceAffordance(handle="a2", role="textbox", label="Password", states=("password",)),
            SurfaceAffordance(handle="a3", role="button", label="Logout"),
        )
    )
    result = LoginForm().recognize(surface)
    assert result is not None
    # The stray "Logout" button must NOT have been bound as the submit handle.
    assert "a3" not in result.matched_handles
    assert not any(s.op == "submit" for s in result.script)


def test_modal_dialog_does_not_select_export_as_close() -> None:
    """An "Export"/"Next" button matches the old bare "x" substring and could be
    chosen as the modal's close target; word-boundary matching excludes it (so a
    dialog with only Export/Next and no real close/confirm does not fire here)."""
    surface = SurfaceView(
        affordances=(
            SurfaceAffordance(handle="a1", role="button", label="Export"),
            SurfaceAffordance(handle="a2", role="button", label="Next"),
        ),
        context=("dialog",),
    )
    assert ModalDialog().recognize(surface) is None


def test_cookie_banner_keeps_confidence_low_with_a_next_link() -> None:
    """A real ACCEPT button plus a "Next"/"Explore" link: under the old substring
    "x" the link was wrongly excluded from ``non_consent`` (shrinking it toward
    <=1 and inflating confidence 0.65 → 0.9). With word-boundary matching the link
    stays in ``non_consent`` and confidence remains 0.65."""
    surface = SurfaceView(
        affordances=(
            SurfaceAffordance(handle="a1", role="button", label="Accept all"),
            SurfaceAffordance(handle="a2", role="link", label="Next"),
            SurfaceAffordance(handle="a3", role="link", label="Explore"),
        ),
        context=("We use cookies for consent",),
    )
    result = CookieBanner().recognize(surface)
    assert result is not None
    assert result.confidence == 0.65
