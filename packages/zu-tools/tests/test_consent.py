"""#94 — WholeWordConsentResolver: whole-word accept, two-step, frame-crossing.

``find()`` is pure over a SurfaceView, so it is unit-tested directly (including
the substring bug the issue calls out). ``dismiss()`` orchestrates perceive/act,
tested over a scripted in-memory ConnectedSurface — plus one integration test
through the real CdpConnectedSurface with the accept button in a CHILD FRAME, to
prove the clear crosses a frame boundary.
"""

from __future__ import annotations

from test_connected_surface import FakeCdpTarget, ax

from zu_core.ports import ConnectedSurface, ConsentResolver, SurfaceAction
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_tools.connected_surface import CdpConnectedSurface
from zu_tools.consent import WholeWordConsentResolver


def aff(handle: str, role: str, label: str, *, value: str | None = None,
        states: tuple[str, ...] = ()) -> SurfaceAffordance:
    return SurfaceAffordance(handle=handle, role=role, label=label, value=value, states=states)


def view(*affs: SurfaceAffordance) -> SurfaceView:
    return SurfaceView(affordances=tuple(affs))


class ScriptedSurface:
    """An in-memory ConnectedSurface: perceive() returns the current view; act()
    swaps to the view registered for (handle, kind), if any."""

    def __init__(self, initial: SurfaceView,
                 transitions: dict[tuple[str, str], SurfaceView] | None = None) -> None:
        self._view = initial
        self._transitions = transitions or {}
        self.acted: list[tuple[str, str, str | None]] = []

    async def perceive(self) -> SurfaceView:
        return self._view

    async def act(self, action: SurfaceAction) -> SurfaceView:
        self.acted.append((action.handle, action.kind, action.text))
        self._view = self._transitions.get((action.handle, action.kind), self._view)
        return self._view


# --- find() -----------------------------------------------------------------

def test_resolver_conforms_to_protocol() -> None:
    assert isinstance(WholeWordConsentResolver(), ConsentResolver)


def test_find_picks_accept_over_manage_and_product_links() -> None:
    v = view(
        aff("a1", "button", "Manage preferences"),
        aff("a2", "link", "Bespoke shirts"),
        aff("a3", "button", "Accept all"),
    )
    ctrl = WholeWordConsentResolver().find(v)
    assert ctrl is not None
    assert ctrl.kind == "accept"
    assert ctrl.handle == "a3"


def test_find_whole_word_ignores_accept_words_inside_product_names() -> None:
    # The substring bug: 'ok' in 'Bespoke', 'yes' in 'Eyes', 'allow' in 'Swallow'.
    # A whole-word matcher must NOT treat any of these as an accept control.
    v = view(
        aff("a1", "link", "Bespoke"),
        aff("a2", "link", "Eyes"),
        aff("a3", "link", "Swallow"),
    )
    assert WholeWordConsentResolver().find(v) is None


def test_find_returns_open_panel_for_two_step_cmp() -> None:
    v = view(aff("a1", "button", "Manage consent"), aff("a2", "button", "Buy now"))
    ctrl = WholeWordConsentResolver().find(v)
    assert ctrl is not None
    assert ctrl.kind == "open_panel"
    assert ctrl.handle == "a1"


def test_find_returns_none_when_no_consent_control() -> None:
    assert WholeWordConsentResolver().find(view(aff("a1", "button", "Add to basket"))) is None


# --- dismiss() --------------------------------------------------------------

async def test_dismiss_one_step_accept_clears_and_reports_true() -> None:
    surface = ScriptedSurface(
        view(aff("a1", "button", "Accept all"), aff("a2", "button", "Buy")),
        transitions={("a1", "click"): view(aff("a2", "button", "Buy"))},
    )
    assert await WholeWordConsentResolver().dismiss(surface) is True
    assert surface.acted == [("a1", "click", None)]


async def test_dismiss_two_step_opens_panel_then_accepts() -> None:
    panel_view = view(aff("b1", "button", "Accept all cookies"), aff("b2", "button", "Buy"))
    surface = ScriptedSurface(
        view(aff("a1", "button", "Manage consent"), aff("a2", "button", "Buy")),
        transitions={
            ("a1", "click"): panel_view,             # open the panel
            ("b1", "click"): view(aff("b2", "button", "Buy")),  # accept in the panel
        },
    )
    assert await WholeWordConsentResolver().dismiss(surface) is True
    assert surface.acted == [("a1", "click", None), ("b1", "click", None)]


async def test_dismiss_gives_up_on_persistent_panel_no_loop() -> None:
    # A footer 'Manage consent' tab that opens no accept: dismiss must NOT report
    # cleared, and must not loop — it makes exactly one open attempt then stops.
    persistent = view(aff("a1", "button", "Manage consent"))
    surface = ScriptedSurface(persistent, transitions={("a1", "click"): persistent})
    assert await WholeWordConsentResolver().dismiss(surface) is False
    assert surface.acted == [("a1", "click", None)]


async def test_dismiss_returns_false_when_no_banner() -> None:
    surface = ScriptedSurface(view(aff("a1", "button", "Add to basket")))
    assert await WholeWordConsentResolver().dismiss(surface) is False
    assert surface.acted == []


async def test_dismiss_clears_accept_button_living_in_a_child_frame() -> None:
    # The failing case from the issue: the accept lives in a (cross-origin) child
    # frame the handle-based enumerator could not reach. CdpConnectedSurface pulls
    # the frame's AX tree and resolves the accept by its global backend node id.
    frames = {
        "": [ax("button", "Buy now", node_id=1)],
        "cmp": [ax("button", "Accept all", node_id=99)],
    }
    tree = {"frame": {"id": "main"}, "childFrames": [{"frame": {"id": "cmp"}}]}
    target = FakeCdpTarget(frames, frame_tree=tree)
    target.effects[99] = target._remove(99)  # accept click clears the CMP frame
    surface: ConnectedSurface = CdpConnectedSurface(target)

    assert await WholeWordConsentResolver().dismiss(surface) is True
    assert ("DOM.resolveNode", {"backendNodeId": 99}) in target.calls
    remaining = [a.label for a in (await surface.perceive()).affordances]
    assert remaining == ["Buy now"]
