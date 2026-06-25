"""The Surface → SurfaceView projection: faithful fields, handle_map dropped."""

from __future__ import annotations

from zu_core.surface import SurfaceView
from zu_tools.action_surface import Affordance, Surface
from zu_tools.surface_adapter import to_surface_view


def test_projection_is_faithful_and_drops_handle_map() -> None:
    surface = Surface(
        title="Sign in",
        url="https://example.test/login",
        affordances=[
            Affordance(handle="a1", role="textbox", label="Email"),
            Affordance(handle="a2", role="textbox", label="Password", states=["required"]),
            Affordance(handle="a3", role="button", label="Sign in"),
        ],
        context=["Welcome back"],
        handle_map={"a1": {"role": "textbox", "name": "Email"}},
        blind=False,
    )
    view = to_surface_view(surface)
    assert isinstance(view, SurfaceView)
    assert view.title == "Sign in"
    assert view.url == "https://example.test/login"
    assert [a.handle for a in view.affordances] == ["a1", "a2", "a3"]
    assert view.affordances[1].states == ("required",)
    assert view.context == ("Welcome back",)
    # handle_map is harness-side indirection and must NOT survive the projection.
    assert not hasattr(view, "handle_map")
    assert "handle_map" not in view.model_dump()


def test_blind_surface_projects_blind_signal() -> None:
    surface = Surface(title="", url="", affordances=[], blind=True, blind_reason="no axtree")
    view = to_surface_view(surface)
    assert view.blind is True
    assert view.blind_reason == "no axtree"
