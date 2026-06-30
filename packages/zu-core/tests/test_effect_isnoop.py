"""#38 — the handle-free no-op oracle (``zu_core.effect.is_noop`` / ``surface_diff``).

``is_noop`` compares two SurfaceViews directly for a silent no-op without needing the
acted handle; ``surface_diff`` is the structured content-free delta it reduces over.
The shared structural core (``_surface_unchanged``) is what ``verify_effect`` already
used — so for the no-change case the two oracles must agree.
"""

from __future__ import annotations

from zu_core.effect import is_noop, surface_diff, verify_effect
from zu_core.surface import SurfaceAffordance, SurfaceView


def _swatch(handle: str, *, states: tuple[str, ...] = (), value: str | None = None) -> SurfaceAffordance:
    return SurfaceAffordance(handle=handle, role="button", label="Red", value=value, states=states)


def test_is_noop_identical_view_is_true() -> None:
    view = SurfaceView(
        title="Shop",
        url="https://shop.test",
        affordances=(_swatch("a1"), SurfaceAffordance(handle="a2", role="link", label="Cart")),
    )
    assert is_noop(view, view) is True
    diff = surface_diff(view, view)
    assert diff == {
        "appeared": (),
        "disappeared": (),
        "state_changed": (),
        "fingerprint_changed": False,
    }


def test_state_flip_same_labels_is_not_noop() -> None:
    before = SurfaceView(affordances=(_swatch("a1", states=()),))
    after = SurfaceView(affordances=(_swatch("a1", states=("selected",)),))
    # Identical label set, but the swatch's own state flipped → a real change.
    assert is_noop(before, after) is False
    diff = surface_diff(before, after)
    assert diff["state_changed"]  # non-empty: the (button, red) identity moved
    assert diff["state_changed"] == (("button", "red"),)
    assert diff["fingerprint_changed"] is True


def test_handle_renumber_only_is_noop() -> None:
    # Same role/label/value/states, only the opaque handle changed (a re-render that
    # renumbered handles). Identity-keyed comparison reads this as no change.
    before = SurfaceView(affordances=(_swatch("a1", states=("selected",), value="v"),))
    after = SurfaceView(affordances=(_swatch("a7", states=("selected",), value="v"),))
    assert is_noop(before, after) is True
    diff = surface_diff(before, after)
    assert diff == {
        "appeared": (),
        "disappeared": (),
        "state_changed": (),
        "fingerprint_changed": False,
    }


def test_label_set_delta_surfaces_in_diff() -> None:
    before = SurfaceView(affordances=(_swatch("a1"),))
    after = SurfaceView(
        affordances=(_swatch("a1"), SurfaceAffordance(handle="a2", role="link", label="View cart"))
    )
    assert is_noop(before, after) is False
    diff = surface_diff(before, after)
    assert diff["appeared"] == ("View cart",)
    assert diff["disappeared"] == ()


def test_is_noop_agrees_with_verify_effect_on_no_change() -> None:
    # For the no-change case, is_noop(b, a) must equal verify_effect saying "silent-no-op"
    # when the acted control is present and its own state is unchanged.
    view = SurfaceView(affordances=(_swatch("a1", states=("selected",), value="v"),))
    # A handle-renumber-only after: structurally identical, acted control unchanged.
    after = SurfaceView(affordances=(_swatch("a9", states=("selected",), value="v"),))
    assert is_noop(view, after) == (
        verify_effect(view, after, acted_handle="a1") == "silent-no-op"
    )
    assert is_noop(view, after) is True
