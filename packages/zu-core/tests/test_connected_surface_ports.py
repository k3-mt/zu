"""#93/#94/#95 — the connected-surface family of ports, at the zu-core seam.

zu-core must not import zu-tools, so conformance is proven here with local dummy
implementations; the reference impls' conformance is asserted in the zu-tools
suite. This test guards the port SHAPES, the interface-version + group
registration, and the frozen value objects.
"""

from __future__ import annotations

import pytest

from zu_core.ports import (
    CheckoutProceeder,
    CheckoutState,
    ConnectedSurface,
    ConsentControl,
    ConsentResolver,
    RequiredSelection,
    SelectionSatisfier,
    SurfaceAction,
)
from zu_core.registry import GROUPS
from zu_core.surface import SurfaceView


class _Surface:
    async def perceive(self) -> SurfaceView:
        return SurfaceView()

    async def act(self, action: SurfaceAction) -> SurfaceView:
        return SurfaceView()


class _Resolver:
    def find(self, view: SurfaceView) -> ConsentControl | None:
        return None

    async def dismiss(self, surface: ConnectedSurface) -> bool:
        return False


class _Satisfier:
    async def satisfy_required(self, surface: ConnectedSurface) -> list[RequiredSelection]:
        return []


class _Proceeder:
    def inspect(self, view: SurfaceView) -> CheckoutState:
        return CheckoutState(in_cart=False, at_checkout=False)

    async def proceed(self, surface: ConnectedSurface) -> bool:
        return False


def test_interface_versions_registered() -> None:
    from zu_core.ports import INTERFACE_VERSION

    assert INTERFACE_VERSION["connected_surfaces"] == 1
    assert INTERFACE_VERSION["consent_resolvers"] == 1
    assert INTERFACE_VERSION["selection_satisfiers"] == 1
    assert INTERFACE_VERSION["checkout_proceeders"] == 1


def test_entry_point_groups_registered() -> None:
    assert GROUPS["connected_surfaces"] == "zu.connected_surfaces"
    assert GROUPS["consent_resolvers"] == "zu.consent_resolvers"
    assert GROUPS["selection_satisfiers"] == "zu.selection_satisfiers"
    assert GROUPS["checkout_proceeders"] == "zu.checkout_proceeders"


def test_protocols_are_structural_and_runtime_checkable() -> None:
    assert isinstance(_Surface(), ConnectedSurface)
    assert isinstance(_Resolver(), ConsentResolver)
    assert isinstance(_Satisfier(), SelectionSatisfier)
    assert isinstance(_Proceeder(), CheckoutProceeder)


def test_value_objects_are_frozen() -> None:
    action = SurfaceAction(handle="a1", kind="click")
    control = ConsentControl(handle="a1", kind="accept", label="Accept all")
    selection = RequiredSelection(handle="a1", chosen_label="Red")
    for obj in (action, control, selection):
        with pytest.raises(Exception):  # noqa: B017 — pydantic frozen raises ValidationError
            obj.handle = "a2"


def test_surface_action_defaults() -> None:
    a = SurfaceAction(handle="a1", kind="type", text="hi")
    assert a.text == "hi"
    assert SurfaceAction(handle="a1", kind="click").text is None


def test_checkout_state_frozen_and_defaults() -> None:
    s = CheckoutState(in_cart=True, at_checkout=False)
    assert s.proceed_handle is None  # default
    with pytest.raises(Exception):  # noqa: B017 — pydantic frozen raises ValidationError
        s.in_cart = False
