"""outcome inference (#69): score a recognised control by GOAL PROGRESS — on-path,
off-path side-quest, or unknown — content-free, from the pattern's declared outcome
vs the goal's tokens. The newsletter is avoided because its OUTCOME ('subscribed')
is not the goal ('basket'), NOT because of its name — which generalises to unnamed
distractions. Offline / $0.
"""

from __future__ import annotations

from typing import Any

from zu_core.ports import RecognitionResult
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_patterns import GoalContext, goal_progress, is_relevant_blocker, is_side_quest
from zu_patterns.cart_checkout import CartCheckout
from zu_patterns.contact_form import ContactForm
from zu_patterns.newsletter_signup import NewsletterSignup


def aff(handle: str, role: str, label: str, **kw: Any) -> SurfaceAffordance:
    return SurfaceAffordance(handle=handle, role=role, label=label, **kw)


# The Conduit goal #69 came from.
_BUY = GoalContext(
    goal="buy a dog collar: pick a colour and size, add to basket, then go "
    "through checkout to the place-order button"
)


def test_newsletter_email_is_off_path_and_not_a_blocker() -> None:
    # The exact #69 trap: a footer newsletter 'ENTER YOUR EMAIL' box (carrying its
    # own HTML required attr). NewsletterSignup recognises it; its declared outcome
    # is 'subscribed', which the buy goal does not want -> OFF-PATH, NOT a blocker.
    footer = SurfaceView(
        affordances=(
            aff("e", "textbox", "Enter your email", states=("required",)),
            aff("b", "button", "Subscribe"),
        )
    )
    rec = NewsletterSignup().recognize(footer)
    assert rec is not None and rec.archetype == "newsletter_signup"
    sig = goal_progress(_BUY, rec)
    assert sig.weight == -1.0 and "off_path" in sig.name  # off-path side-quest
    # The diagnosis-side rule the #41 consumers want: this required widget is NOISE.
    assert is_relevant_blocker(_BUY, rec) is False


def test_cart_checkout_is_on_path_and_a_blocker() -> None:
    cart = SurfaceView(
        affordances=(aff("a", "button", "Add to basket"), aff("c", "link", "Checkout"))
    )
    rec = CartCheckout().recognize(cart)
    assert rec is not None and rec.archetype == "cart_checkout"
    sig = goal_progress(_BUY, rec)
    assert sig.weight == 1.0 and "on_path" in sig.name  # basket/checkout matches the goal
    assert is_relevant_blocker(_BUY, rec) is True


def test_unknown_outcome_is_neutral_not_off_path() -> None:
    # A control that declares no outcome can't be judged off-path -> weight 0,
    # treated conservatively as a possible blocker (never silently dropped).
    rec = RecognitionResult(archetype="modal_dialog", confidence=1.0)  # no outcome
    sig = goal_progress(_BUY, rec)
    assert sig.weight == 0.0 and "unknown" in sig.name
    assert is_relevant_blocker(_BUY, rec) is True


def test_generalises_to_an_unnamed_distraction() -> None:
    # The whole point of #69: outcome inference, not a name-list. A 'spin to win'
    # wheel no pattern was written for, but whose declared outcome is a discount /
    # prize, is OFF-PATH for a buy goal purely from its outcome tokens.
    spin = RecognitionResult(
        archetype="spin_to_win",
        confidence=1.0,
        outcome=("discount", "prize", "spin the wheel", "coupon"),
    )
    assert goal_progress(_BUY, spin).weight == -1.0
    assert is_relevant_blocker(_BUY, spin) is False


def test_signal_is_a_content_free_token_match() -> None:
    # on-path iff the declared outcome overlaps the goal tokens — nothing else; the
    # SAME control is on-path under one goal and off-path under another.
    find = GoalContext(goal="find a blue collar in the search results")
    search = RecognitionResult(archetype="search_box", confidence=1.0, outcome=("search", "results"))
    news = RecognitionResult(archetype="newsletter_signup", confidence=1.0, outcome=("subscribe", "newsletter"))
    assert goal_progress(find, search).weight == 1.0
    assert goal_progress(find, news).weight == -1.0
    # under the BUY goal the search box is merely unknown-vs-off — still off (its
    # outcome doesn't overlap 'buy'), proving relevance is per-goal.
    assert goal_progress(_BUY, search).weight == -1.0


def test_checkout_shipping_form_is_on_path_for_a_buy_goal() -> None:
    # #71 Gap 1: contact_form ALSO fires on a checkout SHIPPING form. Under a buy
    # goal, filling that shipping form is exactly how you reach place-order — so it
    # must be ON-PATH (its required fields are real blockers, not noise), even
    # though the SAME pattern serves a contact-us page.
    checkout = SurfaceView(
        affordances=(
            aff("n", "textbox", "Last name", states=("required",)),
            aff("a", "textbox", "Address line 1"),
            aff("p", "textbox", "Postcode"),
            aff("c", "textbox", "City"),
            aff("s", "button", "Continue"),
        )
    )
    rec = ContactForm().recognize(checkout)
    assert rec is not None and rec.archetype == "contact_form"
    assert goal_progress(_BUY, rec).weight == 1.0  # on-path (checkout/order/address)
    assert is_relevant_blocker(_BUY, rec) is True


def test_terminal_side_quest_vs_navigational_tool() -> None:
    # #71 Gap 2: off-path ALONE is not enough to AVOID a control during navigation.
    # newsletter — TERMINAL: off-path + a dead end ⇒ a side-quest, safe to avoid.
    footer = SurfaceView(
        affordances=(aff("e", "textbox", "Enter your email"), aff("b", "button", "Subscribe"))
    )
    news = NewsletterSignup().recognize(footer)
    assert news is not None and news.terminal is True
    assert goal_progress(_BUY, news).weight == -1.0  # off-path
    assert is_side_quest(_BUY, news) is True  # ⇒ steer around it

    # search — NAVIGATIONAL: off-path by outcome, but a legitimate MEANS to the
    # goal. NOT a side-quest — avoiding it would strand the agent.
    search = RecognitionResult(
        archetype="search_box", confidence=1.0, outcome=("search", "results"), terminal=False
    )
    assert goal_progress(_BUY, search).weight == -1.0  # also off-path…
    assert is_side_quest(_BUY, search) is False  # …but must NOT be avoided

    # an on-path control is never a side-quest, terminal flag or not.
    cart = RecognitionResult(
        archetype="cart_checkout", confidence=1.0, outcome=("basket", "checkout", "order")
    )
    assert is_side_quest(_BUY, cart) is False


def test_unnamed_terminal_distraction_is_a_side_quest() -> None:
    # The behavioural win #69 hinted at but couldn't deliver safely: a novel
    # spin-to-win wheel (no pattern) a consumer marks terminal is a side-quest to
    # steer around — purely from outcome + terminal, no name-list.
    spin = RecognitionResult(
        archetype="spin_to_win",
        confidence=1.0,
        outcome=("discount", "prize", "spin the wheel"),
        terminal=True,
    )
    assert is_side_quest(_BUY, spin) is True
