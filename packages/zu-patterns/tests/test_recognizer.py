"""The recognizer pass + each built-in pattern's recognition — deterministic, $0.

Every assertion feeds a hand-built core SurfaceView and checks archetype +
confidence exactly. No model, no network.
"""

from __future__ import annotations

from zu_core.ports import Pattern
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_patterns.autocomplete import Autocomplete
from zu_patterns.cart_checkout import CartCheckout
from zu_patterns.contact_form import ContactForm
from zu_patterns.cookie_banner import CookieBanner
from zu_patterns.login_form import LoginForm
from zu_patterns.modal_dialog import ModalDialog
from zu_patterns.newsletter_signup import NewsletterSignup
from zu_patterns.paginated_list import PaginatedList
from zu_patterns.recognizer import recognize, record_recognition
from zu_patterns.reversibility import Commitment, classify_action
from zu_patterns.search_box import SearchBox
from zu_patterns.sortable_table import SortableTable

# The eight shipped archetypes, BEFORE #33's two additions — used to prove the
# new patterns leave the existing recognizer behaviour untouched.
EIGHT_PATTERNS: list[Pattern] = [
    CookieBanner(),
    LoginForm(),
    SearchBox(),
    ModalDialog(),
    PaginatedList(),
    SortableTable(),
    Autocomplete(),
    CartCheckout(),
]

ALL_PATTERNS: list[Pattern] = [*EIGHT_PATTERNS, ContactForm(), NewsletterSignup()]


def aff(handle: str, role: str, label: str = "", states: tuple[str, ...] = ()) -> SurfaceAffordance:
    return SurfaceAffordance(handle=handle, role=role, label=label, states=states)


def test_cookie_banner_recognized() -> None:
    view = SurfaceView(
        title="Welcome",
        affordances=(
            aff("a1", "button", "Accept all cookies"),
            aff("a2", "button", "Reject"),
        ),
        context=("We use cookies to improve your experience",),
    )
    r = CookieBanner().recognize(view)
    assert r is not None
    assert r.archetype == "cookie_banner"
    # consent context AND a consent-worded button ⇒ the +0.05 boost over 0.9.
    assert round(r.confidence, 2) == 0.95
    assert r.matched_handles == ("a1",)


def test_login_form_recognized() -> None:
    view = SurfaceView(
        affordances=(
            aff("a1", "textbox", "Email"),
            aff("a2", "textbox", "Password", states=("password",)),
            aff("a3", "button", "Sign in"),
        ),
    )
    r = LoginForm().recognize(view)
    assert r is not None
    assert r.archetype == "login_form"
    assert r.confidence == 0.95
    assert r.matched_handles == ("a1", "a2", "a3")
    assert r.script[-1].op == "submit"


def test_search_box_recognized() -> None:
    view = SurfaceView(affordances=(aff("a1", "searchbox", "Search products"),))
    r = SearchBox().recognize(view)
    assert r is not None
    assert r.archetype == "search_box"
    assert r.confidence == 0.85


def test_search_box_defers_to_login() -> None:
    # a password field present ⇒ search_box must NOT fire (login territory).
    view = SurfaceView(
        affordances=(
            aff("a1", "searchbox", "Search"),
            aff("a2", "textbox", "Password", states=("password",)),
        )
    )
    assert SearchBox().recognize(view) is None


def test_modal_dialog_recognized() -> None:
    view = SurfaceView(
        affordances=(aff("a1", "button", "Close"),),
        context=("Are you sure you want to leave?",),
    )
    r = ModalDialog().recognize(view)
    assert r is not None
    assert r.archetype == "modal_dialog"


def test_paginated_list_recognized() -> None:
    view = SurfaceView(
        affordances=(
            aff("a1", "list", "Results"),
            aff("a2", "link", "Next page"),
        ),
    )
    r = PaginatedList().recognize(view)
    assert r is not None
    assert r.confidence == 0.8


def test_sortable_table_recognized() -> None:
    view = SurfaceView(
        affordances=(
            aff("a1", "grid", "Orders"),
            aff("a2", "columnheader", "Date", states=("sortable",)),
        ),
    )
    r = SortableTable().recognize(view)
    assert r is not None
    assert r.confidence == 0.8


def test_autocomplete_recognized() -> None:
    view = SurfaceView(
        affordances=(
            aff("a1", "combobox", "City", states=("expanded",)),
            aff("a2", "option", "London"),
            aff("a3", "option", "Lyon"),
        ),
    )
    r = Autocomplete().recognize(view)
    assert r is not None
    assert r.confidence == 0.85
    assert r.matched_handles == ("a1", "a2")


def test_cart_checkout_recognized_stops_before_commit() -> None:
    view = SurfaceView(
        affordances=(
            aff("a1", "button", "Add to cart"),
            aff("a2", "button", "Place order"),
        ),
        context=("Subtotal: $42.00", "Order summary"),
    )
    r = CartCheckout().recognize(view)
    assert r is not None
    assert r.confidence == 0.85
    # the proposed script never proposes EXECUTING the place-order step.
    ops = [s.op for s in r.script]
    assert "submit" not in ops and "click" in ops
    # the commit boundary is marked as an expect (a boundary), not a click.
    commit_steps = [s for s in r.script if "COMMIT BOUNDARY" in s.note]
    assert commit_steps and commit_steps[0].op == "expect"


def test_recognizer_picks_best_and_falls_through_on_low_confidence() -> None:
    view = SurfaceView(
        affordances=(
            aff("a1", "textbox", "Email"),
            aff("a2", "textbox", "Password", states=("password",)),
            aff("a3", "button", "Sign in"),
        ),
    )
    rec = recognize(view, ALL_PATTERNS, min_confidence=0.6)
    assert rec.result is not None
    assert rec.result.archetype == "login_form"
    # candidates are confidence-sorted.
    confs = [c.confidence for c in rec.candidates]
    assert confs == sorted(confs, reverse=True)


def test_recognizer_no_hint_when_below_threshold() -> None:
    # an empty surface matches nothing ⇒ no result, no candidates.
    rec = recognize(SurfaceView(), ALL_PATTERNS)
    assert rec.result is None
    assert rec.candidates == ()


def test_recognizer_threshold_gates_a_weak_hit() -> None:
    # a bare textbox labelled "search" matches search_box weakly (0.62); raise the
    # threshold above it ⇒ no confident result (fall through to the model).
    view = SurfaceView(affordances=(aff("a1", "textbox", "Search the site"),))
    rec = recognize(view, ALL_PATTERNS, min_confidence=0.7)
    assert rec.result is None
    assert any(c.archetype == "search_box" for c in rec.candidates)


def test_record_recognition_payload_shape() -> None:
    r = LoginForm().recognize(
        SurfaceView(
            affordances=(
                aff("a1", "textbox", "Email"),
                aff("a2", "textbox", "Password", states=("password",)),
                aff("a3", "button", "Sign in"),
            )
        )
    )
    assert r is not None
    payload = record_recognition(r, blind=False)
    assert payload["archetype"] == "login_form"
    assert payload["matched_handles"] == ["a1", "a2", "a3"]
    assert payload["blind"] is False
    assert isinstance(payload["confidence"], float)


# --- #33: contact_form + newsletter_signup -----------------------------------


def test_contact_form_recognized_shipping_slots() -> None:
    # email + postcode + city + name ⇒ contact_form (~0.85), NOT newsletter.
    view = SurfaceView(
        affordances=(
            aff("a1", "textbox", "Email"),
            aff("a2", "textbox", "Postcode"),
            aff("a3", "textbox", "City"),
            aff("a4", "textbox", "Full name"),
            aff("a5", "button", "Submit"),
        ),
    )
    r = ContactForm().recognize(view)
    assert r is not None
    assert r.archetype == "contact_form"
    assert round(r.confidence, 2) == 0.85
    assert r.script[-1].op == "submit"
    # mutually exclusive: a shipping form is never a newsletter signup.
    assert NewsletterSignup().recognize(view) is None


def test_newsletter_signup_lone_email_with_button() -> None:
    # a lone email + a 'Subscribe' button ⇒ newsletter_signup, NOT contact_form.
    view = SurfaceView(
        affordances=(
            aff("a1", "textbox", "Email"),
            aff("a2", "button", "Subscribe"),
        ),
    )
    r = NewsletterSignup().recognize(view)
    assert r is not None
    assert r.archetype == "newsletter_signup"
    assert round(r.confidence, 2) == 0.85
    # a lone email (<=2 fields, no shipping/OTP) is NOT a contact form.
    assert ContactForm().recognize(view) is None


def test_contact_form_otp_branch() -> None:
    # a lone OTP field ⇒ contact_form via the OTP branch (~0.85), not newsletter.
    view = SurfaceView(affordances=(aff("a1", "textbox", "One-time passcode"),))
    r = ContactForm().recognize(view)
    assert r is not None
    assert r.archetype == "contact_form"
    assert round(r.confidence, 2) == 0.85
    assert NewsletterSignup().recognize(view) is None
    # the OTP tell also fires off a free-form state, not just the label.
    by_state = SurfaceView(affordances=(aff("b1", "textbox", "Code", states=("otp",)),))
    assert ContactForm().recognize(by_state) is not None


def test_contact_form_three_field_fallback() -> None:
    # 3 unrelated textboxes, no shipping/OTP/subscribe vocab ⇒ contact_form ~0.62.
    view = SurfaceView(
        affordances=(
            aff("a1", "textbox", "Field A"),
            aff("a2", "textbox", "Field B"),
            aff("a3", "textbox", "Field C"),
        ),
    )
    r = ContactForm().recognize(view)
    assert r is not None
    assert r.archetype == "contact_form"
    assert round(r.confidence, 2) == 0.62
    # two unrelated fields is below the fallback floor ⇒ no match.
    two = SurfaceView(
        affordances=(aff("a1", "textbox", "Field A"), aff("a2", "textbox", "Field B"))
    )
    assert ContactForm().recognize(two) is None


def test_newsletter_context_only_confidence() -> None:
    # a lone email with subscribe CONTEXT but no worded button ⇒ ~0.65.
    view = SurfaceView(
        affordances=(
            aff("a1", "textbox", "Email"),
            aff("a2", "button", "Go"),
        ),
        context=("Subscribe to our newsletter for updates",),
    )
    r = NewsletterSignup().recognize(view)
    assert r is not None
    assert r.archetype == "newsletter_signup"
    assert round(r.confidence, 2) == 0.65


def test_contact_form_commit_prior_flags_submit() -> None:
    # submit = COMMITTING, wired through classify_action (not a core constant).
    prior = (ContactForm.commit_prior(),)
    assert classify_action(op="submit", priors=prior) is Commitment.COMMITTING
    # a plain fill on a textbox stays REVERSIBLE — the prior is submit-scoped.
    assert classify_action(op="fill", role="textbox", priors=prior) is Commitment.REVERSIBLE


def test_newsletter_submit_prior_reversible_leaning() -> None:
    # subscribe = REVERSIBLE-leaning: the prior overcomes the submit op-signal.
    prior = (NewsletterSignup.submit_prior(),)
    assert classify_action(op="submit", priors=prior) is Commitment.REVERSIBLE


def test_contact_form_defers_to_login_password() -> None:
    # a login surface (password field) keeps login_form's territory: the password
    # field is excluded from the fillable count, so the >=3 fallback never fires.
    view = SurfaceView(
        affordances=(
            aff("a1", "textbox", "Email"),
            aff("a2", "textbox", "Password", states=("password",)),
            aff("a3", "button", "Sign in"),
        ),
    )
    assert ContactForm().recognize(view) is None


def test_existing_eight_archetypes_unaffected() -> None:
    # Re-run the recognizer with the two new patterns added over each existing
    # fixture; the winning archetype + exact confidence must be unchanged.
    cases = {
        "login_form": (
            0.95,
            SurfaceView(
                affordances=(
                    aff("a1", "textbox", "Email"),
                    aff("a2", "textbox", "Password", states=("password",)),
                    aff("a3", "button", "Sign in"),
                )
            ),
        ),
        "search_box": (
            0.85,
            SurfaceView(affordances=(aff("a1", "searchbox", "Search products"),)),
        ),
        "cart_checkout": (
            0.85,
            SurfaceView(
                affordances=(
                    aff("a1", "button", "Add to cart"),
                    aff("a2", "button", "Place order"),
                ),
                context=("Subtotal: $42.00", "Order summary"),
            ),
        ),
    }
    for archetype, (conf, view) in cases.items():
        before = recognize(view, EIGHT_PATTERNS)
        after = recognize(view, ALL_PATTERNS)
        assert before.result is not None and after.result is not None
        assert before.result.archetype == archetype
        assert after.result.archetype == archetype
        assert round(after.result.confidence, 2) == conf


def test_default_bundle_exports() -> None:
    import zu_patterns

    assert zu_patterns.ContactForm is ContactForm
    assert zu_patterns.NewsletterSignup is NewsletterSignup
    assert "ContactForm" in zu_patterns.__all__
    assert "NewsletterSignup" in zu_patterns.__all__
