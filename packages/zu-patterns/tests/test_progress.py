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
from zu_patterns import GoalContext, goal_progress, is_relevant_blocker
from zu_patterns.cart_checkout import CartCheckout
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
