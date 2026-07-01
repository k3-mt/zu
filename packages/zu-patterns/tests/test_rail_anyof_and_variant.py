"""Issues #46 & #39 — content-free verification rails hardened.

#46: success/failure rails take an ANY-OF token set with normalized, word-boundary
matching, so a casing/synonym variant of the expected marker satisfies the rail
("Payment received" is a checkout success; "Card declined" fires the login failure)
while a DECOY that merely contains a token as a substring does NOT falsely satisfy.
These FAIL against the old single-literal exact-set-membership rail.

#39: a content-free "a control became selected" success invariant, plus a
structural ``VariantPicker`` recognizer. The invariant fires when a previously-
unselected option's ``states`` flips to selected (identical labels), reusing the
#38 state-delta primitives; the recognizer fires on a selectable GROUP, not a plain
content page. FAIL against the old code (no such pattern/invariant/state key).

All offline, $0: hand-built SurfaceViews + synthetic event logs (mirror
test_pattern_rail.py / test_invariants.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from zu_core import events as ev
from zu_core.invariants import compile_spec
from zu_core.ports import MonitorState, RunContext
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_patterns.cart_checkout import CartCheckout
from zu_patterns.login_form import LoginForm
from zu_patterns.variant_picker import VariantPicker


def _ev(etype: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type=etype, payload=payload)


def _ctx(events: list) -> RunContext:
    return cast(RunContext, SimpleNamespace(events=events))


_DEADLINE = _ev(ev.TASK_TERMINAL, {"reason": "done"})


def _cart_view() -> SurfaceView:
    return SurfaceView(
        affordances=(SurfaceAffordance(handle="b1", role="button", label="Checkout"),),
        context=("cart", "subtotal"),
    )


def _login_view() -> SurfaceView:
    return SurfaceView(
        affordances=(
            SurfaceAffordance(handle="a1", role="textbox", label="Email"),
            SurfaceAffordance(handle="a2", role="textbox", label="Password", states=("password",)),
            SurfaceAffordance(handle="a3", role="button", label="Sign in"),
        )
    )


# --- #46 -----------------------------------------------------------------


def test_cart_success_rail_accepts_a_synonym_variant() -> None:
    """A genuinely successful checkout whose surface reads "Payment received" (NOT
    the literal "order confirmed") does NOT violate the liveness success rail at the
    deadline — the any-of confirmation vocabulary satisfies it."""
    cc = CartCheckout()
    result = cc.recognize(_cart_view())
    assert result is not None
    monitor = compile_spec(cc.success_invariants(result))[0]
    good = [
        _ev(ev.SURFACE_CAPTURED, {"labels": ["Payment received"]}),
        _DEADLINE,
    ]
    assert monitor.evaluate(_ctx(good)) is None


def test_cart_success_decoy_substring_does_not_satisfy() -> None:
    """A DECOY label that merely CONTAINS a success token as a substring
    ("Reconfirmation settings" contains "confirmation") does NOT satisfy — so a
    non-success surface still VIOLATES the liveness at the deadline."""
    cc = CartCheckout()
    result = cc.recognize(_cart_view())
    assert result is not None
    monitor = compile_spec(cc.success_invariants(result))[0]
    decoy = [
        _ev(ev.SURFACE_CAPTURED, {"labels": ["Reconfirmation settings"]}),
        _DEADLINE,
    ]
    verdict = monitor.evaluate(_ctx(decoy))
    assert verdict is not None and verdict.state is MonitorState.VIOLATION


def test_login_failure_rail_fires_on_a_real_decline() -> None:
    """A real failure surface reading "Card declined" (not the literal "error")
    fires the negated-THROUGHOUT failure rail — a failure is no longer silently
    missed. A decoy substring ("Terrorem") does NOT fire it."""
    lf = LoginForm()
    result = lf.recognize(_login_view())
    assert result is not None
    fmon = compile_spec(lf.failure_invariants(result))[0]

    errored = [_ev(ev.SURFACE_CAPTURED, {"labels": ["Card declined"]})]
    verdict = fmon.evaluate(_ctx(errored))
    assert verdict is not None and verdict.state is MonitorState.VIOLATION

    # A word merely CONTAINING "error" as a substring must not fire the failure rail.
    not_error = [_ev(ev.SURFACE_CAPTURED, {"labels": ["Terrorem"]})]
    assert fmon.evaluate(_ctx(not_error)) is None


def test_login_success_rail_accepts_all_account_synonyms() -> None:
    """"Sign out" and "Account" (the two synonyms the old ``[:1]`` truncation
    silently discarded) each satisfy the login success rail, as does "Logout"."""
    lf = LoginForm()
    result = lf.recognize(_login_view())
    assert result is not None
    smon = compile_spec(lf.success_invariants(result))[0]
    for tok in ("Sign out", "Account", "Logout"):
        log = [_ev(ev.SURFACE_CAPTURED, {"labels": [tok]}), _DEADLINE]
        assert smon.evaluate(_ctx(log)) is None, tok


# --- #39 -----------------------------------------------------------------


def _picker_view() -> SurfaceView:
    # A structural swatch/variant group — selectable roles, PRODUCT-NAME labels the
    # recognizer must NOT depend on.
    return SurfaceView(
        affordances=(
            SurfaceAffordance(handle="s1", role="radio", label="Red"),
            SurfaceAffordance(handle="s2", role="radio", label="Blue"),
            SurfaceAffordance(handle="s3", role="radio", label="Green"),
        )
    )


def test_variant_picker_recognizes_a_selectable_group() -> None:
    result = VariantPicker().recognize(_picker_view())
    assert result is not None
    assert result.archetype == "variant_picker"
    assert result.matched_handles == ("s1", "s2", "s3")
    # A content-free, non-empty success invariant list a consumer can evaluate.
    assert VariantPicker().success_invariants(result)


def test_variant_picker_does_not_fire_on_a_plain_content_page() -> None:
    plain = SurfaceView(
        affordances=(
            SurfaceAffordance(handle="p1", role="link", label="Home"),
            SurfaceAffordance(handle="p2", role="link", label="About"),
            SurfaceAffordance(handle="p3", role="paragraph", label="Welcome"),
        )
    )
    assert VariantPicker().recognize(plain) is None
    # A lone selectable control is a toggle, not a picker group.
    lone = SurfaceView(affordances=(SurfaceAffordance(handle="c1", role="radio", label="One"),))
    assert VariantPicker().recognize(lone) is None


def test_became_selected_invariant_fires_on_state_flip() -> None:
    """The content-free "control became selected" success: a previously-unselected
    option's ``states`` flips to selected with IDENTICAL labels → the invariant is
    satisfied (keyed on the state transition, not page text)."""
    vp = VariantPicker()
    result = vp.recognize(_picker_view())
    assert result is not None
    monitor = compile_spec(vp.success_invariants(result))[0]
    acted = result.matched_handles[0]

    before = {
        "affordances": [
            {"handle": "s1", "label": "Red", "states": []},
            {"handle": "s2", "label": "Blue", "states": []},
        ]
    }
    after = {
        "affordances": [
            {"handle": acted, "label": "Red", "states": ["selected"]},
            {"handle": "s2", "label": "Blue", "states": []},
        ]
    }
    seq = [
        _ev(ev.SURFACE_CAPTURED, before),
        _ev(ev.SURFACE_CAPTURED, after),
        _DEADLINE,
    ]
    # No VIOLATION at any prefix — inert pre-selection, satisfied once selected.
    for i in range(1, len(seq) + 1):
        assert monitor.evaluate(_ctx(seq[:i])) is None, f"fired at prefix len {i}"


def test_became_selected_covers_checked_synonym() -> None:
    """A picker that flips ``checked`` rather than literally ``selected`` still
    satisfies "became selected" (the any-of state set)."""
    vp = VariantPicker()
    result = vp.recognize(_picker_view())
    assert result is not None
    monitor = compile_spec(vp.success_invariants(result))[0]
    acted = result.matched_handles[0]
    seq = [
        _ev(ev.SURFACE_CAPTURED, {"affordances": [{"handle": acted, "label": "Red", "states": ["checked"]}]}),
        _DEADLINE,
    ]
    assert monitor.evaluate(_ctx(seq)) is None


def test_became_selected_silent_noop_violates_at_deadline() -> None:
    """The silent no-op — the click was accepted but NO variant got selected — the
    acted option never reaches a selected state by the deadline → VIOLATION."""
    vp = VariantPicker()
    result = vp.recognize(_picker_view())
    assert result is not None
    monitor = compile_spec(vp.success_invariants(result))[0]
    acted = result.matched_handles[0]
    noop = [
        _ev(ev.SURFACE_CAPTURED, {"affordances": [{"handle": acted, "label": "Red", "states": []}]}),
        _DEADLINE,
    ]
    verdict = monitor.evaluate(_ctx(noop))
    assert verdict is not None and verdict.state is MonitorState.VIOLATION
