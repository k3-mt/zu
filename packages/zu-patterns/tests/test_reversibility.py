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
    # Generic INTERACTION primitives are committing-leaning — NOT commerce verbs.
    assert classify_action(op="submit") is Commitment.COMMITTING
    assert classify_action(op="confirm") is Commitment.COMMITTING
    assert classify_action(op="delete") is Commitment.COMMITTING


def test_no_commerce_verb_blocklist_in_classifier() -> None:
    # #65 F16: the classifier no longer hardcodes a commerce-verb blocklist. A bare
    # commerce verb carries NO op-signal of its own (it is not an interaction
    # primitive) — it falls to the default-committing FLOOR (so still safe), but is
    # NOT committing BECAUSE of a "pay"/"checkout"/"place_order" keyword. The proof
    # a keyword blocklist is gone: pairing the verb with a reversible role/method
    # that WOULD have been overridden by a committing op-keyword now lands REVERSIBLE.
    from zu_patterns.reversibility import _COMMITTING_OPS

    for verb in ("pay", "checkout", "place_order", "purchase"):
        assert verb not in _COMMITTING_OPS
        # a "pay" GET (a reversible method) is reversible — no keyword forces commit.
        assert classify_action(op=verb, http_method="GET") is Commitment.REVERSIBLE
    # the commerce commit boundary is instead declared by the cart pattern's prior.
    prior = CartCheckout.commit_prior()
    assert (
        classify_action(op="place_order", http_method="GET", priors=(prior,))
        is Commitment.COMMITTING
    )


def test_submits_is_primary_structural_irreversibility_signal() -> None:
    # #65 F16: a control that STRUCTURALLY declares a side effect (submits — a
    # button[type=submit]/form-submit) is committing by SHAPE, no label word needed.
    assert classify_action(submits=True) is Commitment.COMMITTING
    # it dominates a single reversible role hint: a SUBMIT control rendered as a
    # link/tab is still committing (this is the F16 replacement for the verb list).
    assert classify_action(role="link", submits=True) is Commitment.COMMITTING
    assert classify_action(role="tab", submits=True) is Commitment.COMMITTING


def test_link_tab_reversible_only_for_plain_navigation() -> None:
    # #65 F18: a link/tab is reversible for PLAIN navigation …
    assert classify_action(role="link") is Commitment.REVERSIBLE
    assert classify_action(role="tab") is Commitment.REVERSIBLE
    # … but a committing navigation (a logout/delete link/tab that submits) is NOT
    # assumed reversible — decided by the structural ``submits`` signal, no words.
    assert classify_action(role="link", submits=True) is Commitment.COMMITTING
    assert classify_action(role="tab", submits=True) is Commitment.COMMITTING


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
