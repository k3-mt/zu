"""The core SurfaceView currency — pure, frozen, round-trippable."""

from __future__ import annotations

import pytest

from zu_core.surface import SurfaceAffordance, SurfaceView


def test_surface_view_round_trips() -> None:
    view = SurfaceView(
        title="Sign in",
        url="https://example.test/login",
        affordances=(
            SurfaceAffordance(handle="a1", role="textbox", label="Email"),
            SurfaceAffordance(
                handle="a2", role="textbox", label="Password", states=("required",)
            ),
            SurfaceAffordance(handle="a3", role="button", label="Sign in"),
        ),
        context=("Sign in to your account",),
    )
    again = SurfaceView.model_validate(view.model_dump())
    assert again == view
    assert again.affordances[1].states == ("required",)


def test_surface_view_is_frozen_and_hashable() -> None:
    aff = SurfaceAffordance(handle="a1", role="button", label="OK")
    with pytest.raises((TypeError, ValueError)):
        aff.handle = "a2"
    # tuple states ⇒ the affordance is hashable
    assert hash(aff) == hash(SurfaceAffordance(handle="a1", role="button", label="OK"))


def test_surface_view_defaults_are_empty() -> None:
    v = SurfaceView()
    assert v.affordances == ()
    assert v.context == ()
    assert v.blind is False
    assert v.url == ""
