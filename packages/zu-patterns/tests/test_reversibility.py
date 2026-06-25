"""The reversible-vs-committing classifier — principled, default-to-committing."""

from __future__ import annotations

from zu_patterns.cart_checkout import CartCheckout
from zu_patterns.reversibility import (
    ActionPrior,
    Commitment,
    classify_action,
)


def test_get_is_reversible() -> None:
    assert classify_action(http_method="GET") is Commitment.REVERSIBLE


def test_post_is_committing() -> None:
    assert classify_action(http_method="POST") is Commitment.COMMITTING
    assert classify_action(http_method="DELETE") is Commitment.COMMITTING


def test_unknown_defaults_to_committing() -> None:
    # no signal at all ⇒ default-to-safe (committing).
    assert classify_action() is Commitment.COMMITTING
    # a bare ambiguous button ⇒ still committing (no resolving signal).
    assert classify_action(role="button") is Commitment.COMMITTING


def test_explicit_annotation_wins() -> None:
    # the rail annotation is authoritative even against a committing method.
    assert (
        classify_action(http_method="POST", annotations={"consequence": "read"})
        is Commitment.REVERSIBLE
    )
    assert (
        classify_action(http_method="GET", annotations={"consequence": "payment"})
        is Commitment.COMMITTING
    )


def test_reversible_op_and_role() -> None:
    assert classify_action(op="fill", role="textbox") is Commitment.REVERSIBLE
    assert classify_action(op="read") is Commitment.REVERSIBLE


def test_committing_op() -> None:
    assert classify_action(op="pay") is Commitment.COMMITTING
    assert classify_action(op="place_order") is Commitment.COMMITTING


def test_idempotent_flag_shifts_reversible() -> None:
    assert classify_action(idempotent=True) is Commitment.REVERSIBLE
    assert classify_action(idempotent=False) is Commitment.COMMITTING


def test_contributed_prior_makes_action_committing() -> None:
    # a pattern contributes a prior that flips an otherwise-reversible action.
    prior = ActionPrior(
        name="x.danger",
        matcher=lambda f: f.get("op") == "fill",
        commitment=Commitment.COMMITTING,
        weight=5.0,
    )
    # op=fill alone is reversible; the heavy committing prior overrides.
    assert classify_action(op="fill") is Commitment.REVERSIBLE
    assert classify_action(op="fill", priors=(prior,)) is Commitment.COMMITTING


def test_cart_checkout_commit_prior_flags_place_order() -> None:
    prior = CartCheckout.commit_prior()
    # the place-order step (by op or label) classifies COMMITTING via the prior.
    assert classify_action(op="place_order", priors=(prior,)) is Commitment.COMMITTING
    # a plain fill stays reversible even with the prior present.
    assert classify_action(op="fill", role="textbox", priors=(prior,)) is Commitment.REVERSIBLE
