"""#93 — CdpConnectedSurface over a fake external CDP target, fully offline ($0).

No browser: a ``FakeCdpTarget`` answers the handful of CDP methods the surface
uses (``Page.getFrameTree``, ``Accessibility.getFullAXTree``,
``Target.getTargetInfo``, ``DOM.resolveNode``, ``Runtime.callFunctionOn``) from an
in-memory, mutable set of AX-tree snapshots — so ``perceive → act → perceive``
loops run for real. This mirrors ``test_action_surface``'s fake-session pattern.
"""

from __future__ import annotations

from typing import Any

from zu_core.ports import ConnectedSurface, SurfaceAction
from zu_tools.connected_surface import CdpConnectedSurface


def ax(role: str, name: str = "", *, node_id: int | None = None,
       states: tuple[str, ...] = (), value: str | None = None) -> dict:
    """Build one raw CDP ``getFullAXTree`` node (the ``{type,value}`` shape)."""
    node: dict[str, Any] = {"role": {"value": role}, "name": {"value": name}, "ignored": False}
    if node_id is not None:
        node["backendDOMNodeId"] = node_id
    if value is not None:
        node["value"] = {"value": value}
    node["properties"] = [{"name": s, "value": {"value": True}} for s in states]
    return node


def _snapshot(bounds: dict[int, list[float]]) -> dict:
    """A minimal ``DOMSnapshot.captureSnapshot`` response for a box map: one document
    whose ``layout.nodeIndex`` points at ``nodes.backendNodeId`` and whose
    ``layout.bounds`` are the [x,y,w,h] boxes — the exact shape the surface decodes."""
    backend_ids = list(bounds)
    return {
        "documents": [{
            "nodes": {"backendNodeId": backend_ids},
            "layout": {
                "nodeIndex": list(range(len(backend_ids))),
                "bounds": [bounds[b] for b in backend_ids],
            },
        }],
        "strings": [],
    }


class FakeCdpTarget:
    """A minimal in-memory CDP endpoint. ``frames`` maps a frame id ("" = main) to
    its current AX node list; ``effects`` maps a backend node id to a callback that
    mutates ``frames`` when that node is acted on — the DOM effect a browser would
    apply, modelled at the AX level."""

    def __init__(self, frames: dict[str, list[dict]], *, title: str = "", url: str = "",
                 frame_tree: dict | None = None,
                 bounds: dict[int, list[float]] | None = None) -> None:
        self.frames = frames
        self.title = title
        self.url = url
        self.frame_tree = frame_tree
        # backendDOMNodeId -> [x, y, w, h]; served as a DOMSnapshot.captureSnapshot so
        # the surface stamps real geometry and prunes zero-area controls. None => the
        # method returns nothing (the pre-fix fail-open path: every node kept).
        self.bounds = bounds
        self.calls: list[tuple[str, dict]] = []
        self.effects: dict[int, Any] = {}
        self._obj_to_node: dict[str, Any] = {}

    async def send(
        self, method: str, params: dict | None = None, *, session_id: str | None = None
    ) -> dict:
        params = params or {}
        self.calls.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": self.frame_tree} if self.frame_tree else {}
        if method == "Accessibility.getFullAXTree":
            return {"nodes": list(self.frames.get(params.get("frameId", ""), []))}
        if method == "DOMSnapshot.captureSnapshot":
            return _snapshot(self.bounds) if self.bounds is not None else {}
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"title": self.title, "url": self.url}}
        if method == "DOM.resolveNode":
            bid = params.get("backendNodeId")
            oid = f"obj-{bid}"
            self._obj_to_node[oid] = bid
            return {"object": {"objectId": oid}}
        if method == "Runtime.callFunctionOn":
            bid = self._obj_to_node.get(params.get("objectId", ""))
            effect = self.effects.get(bid) if bid is not None else None
            if effect is not None:
                effect(self, params)
            return {"result": {"value": None}}
        return {}

    def _remove(self, node_id: int) -> Any:
        def effect(target: FakeCdpTarget, _params: dict) -> None:
            for nodes in target.frames.values():
                nodes[:] = [n for n in nodes if n.get("backendDOMNodeId") != node_id]
        return effect


def _labels(view: Any) -> list[str]:
    return [a.label for a in view.affordances]


def test_surface_conforms_to_connected_surface_protocol() -> None:
    assert isinstance(CdpConnectedSurface(FakeCdpTarget({})), ConnectedSurface)


async def test_perceive_flattens_across_frames_and_dedupes_by_backend_node() -> None:
    # Main frame carries Accept (node 10) + a heading; a child frame carries Place
    # order (node 20) AND a duplicate of Accept (node 10, as a same-origin frame
    # already flattened in the main tree would). Perceive must union the frames yet
    # count node 10 once.
    frames = {
        "": [ax("button", "Accept all", node_id=10), ax("heading", "Cart")],
        "f2": [ax("button", "Place order", node_id=20), ax("button", "Accept all", node_id=10)],
    }
    tree = {"frame": {"id": "main"}, "childFrames": [{"frame": {"id": "f2"}}]}
    surface = CdpConnectedSurface(FakeCdpTarget(frames, title="Shop", url="https://shop.test", frame_tree=tree))

    view = await surface.perceive()

    assert view.title == "Shop"
    assert view.url == "https://shop.test"
    assert _labels(view) == ["Accept all", "Place order"]  # deduped, both frames present
    assert "Cart" in view.context
    # The child frame's AX tree was pulled by frame id, the main one with no id.
    target: FakeCdpTarget = surface._target  # type: ignore[assignment]
    ax_calls = [p.get("frameId", "") for m, p in target.calls if m == "Accessibility.getFullAXTree"]
    assert "" in ax_calls and "f2" in ax_calls


async def test_perceive_prunes_invisible_controls_using_real_geometry() -> None:
    # The #93/#126 bug on urban.co: a collapsed mega-menu link is ignored=false and 0x0,
    # so getFullAXTree (no geometry) floods the surface and the model clicks an invisible
    # 'Massage in Manchester' no-op. With a real box captured per session, perceive()
    # drops it — while an on-screen and a BELOW-THE-FOLD control both survive.
    frames = {"": [
        ax("link", "Massage in Manchester", node_id=1),   # collapsed menu, 0x0
        ax("button", "Book now", node_id=2),               # on-screen
        ax("button", "Footer", node_id=3),                 # below the fold, real size
    ]}
    bounds = {
        1: [10.0, 20.0, 0.0, 0.0],
        2: [10.0, 100.0, 120.0, 40.0],
        3: [10.0, 9000.0, 120.0, 40.0],  # large y, real w/h — a scroll away, not hidden
    }
    surface = CdpConnectedSurface(FakeCdpTarget(frames, bounds=bounds))
    view = await surface.perceive()
    assert _labels(view) == ["Book now", "Footer"]  # invisible link gone, below-fold kept


async def test_perceive_fails_open_when_geometry_is_unavailable() -> None:
    # No DOMSnapshot support (bounds=None => the method returns nothing): every control
    # keeps bounds=None and survives — never hide a real control on a geometry gap.
    frames = {"": [ax("link", "Massage in Manchester", node_id=1), ax("button", "Book", node_id=2)]}
    surface = CdpConnectedSurface(FakeCdpTarget(frames, bounds=None))
    view = await surface.perceive()
    assert _labels(view) == ["Massage in Manchester", "Book"]


async def test_perceive_falls_back_to_main_frame_when_no_frame_tree() -> None:
    frames = {"": [ax("button", "Buy", node_id=1)]}
    surface = CdpConnectedSurface(FakeCdpTarget(frames))
    view = await surface.perceive()
    assert _labels(view) == ["Buy"]


async def test_act_click_resolves_backend_node_and_reperceives_effect() -> None:
    frames = {"": [ax("button", "Accept all", node_id=10), ax("button", "Buy", node_id=11)]}
    target = FakeCdpTarget(frames)
    target.effects[10] = target._remove(10)  # clicking Accept removes it
    surface = CdpConnectedSurface(target)

    view = await surface.perceive()
    accept = next(a for a in view.affordances if a.label == "Accept all")
    after = await surface.act(SurfaceAction(handle=accept.handle, kind="click"))

    assert _labels(after) == ["Buy"]  # accept cleared, re-perceived
    # It resolved the handle to its GLOBAL backend node id, then called a click fn.
    assert ("DOM.resolveNode", {"backendNodeId": 10}) in target.calls
    click = next(p for m, p in target.calls if m == "Runtime.callFunctionOn")
    assert "this.click()" in click["functionDeclaration"]


async def test_act_type_sends_value_through_native_setter() -> None:
    frames = {"": [ax("textbox", "Email", node_id=30)]}
    target = FakeCdpTarget(frames)
    surface = CdpConnectedSurface(target)
    view = await surface.perceive()
    box = view.affordances[0]

    await surface.act(SurfaceAction(handle=box.handle, kind="type", text="a@b.co"))

    call = next(p for m, p in target.calls if m == "Runtime.callFunctionOn")
    assert "dispatchEvent" in call["functionDeclaration"]
    assert call["arguments"] == [{"value": "a@b.co"}]


async def test_act_select_uses_option_picker_and_reflects_new_value() -> None:
    frames = {"": [ax("combobox", "Colour", node_id=40, states=("required",), value="Choose an option")]}
    target = FakeCdpTarget(frames)

    def choose_red(t: FakeCdpTarget, _params: dict) -> None:
        t.frames[""][0]["value"] = {"value": "Red"}
    target.effects[40] = choose_red
    surface = CdpConnectedSurface(target)

    view = await surface.perceive()
    combo = view.affordances[0]
    after = await surface.act(SurfaceAction(handle=combo.handle, kind="select", text=None))

    call = next(p for m, p in target.calls if m == "Runtime.callFunctionOn")
    assert "options" in call["functionDeclaration"]  # the deterministic option picker
    assert call["arguments"] == [{"value": None}]     # None => first valid option
    assert after.affordances[0].value == "Red"


async def test_act_on_stale_handle_is_a_reperceive_not_a_crash() -> None:
    frames = {"": [ax("button", "Buy", node_id=1)]}
    target = FakeCdpTarget(frames)
    surface = CdpConnectedSurface(target)
    await surface.perceive()

    view = await surface.act(SurfaceAction(handle="a99", kind="click"))

    assert _labels(view) == ["Buy"]  # unchanged, current truth
    assert not any(m == "DOM.resolveNode" for m, _ in target.calls)  # nothing resolved


async def test_perceive_keeps_an_unnamed_select_variant() -> None:
    # #110: a WooCommerce variant <select> has no accessible name. perceive() must
    # still surface it as a combobox (resolvable by node id), not drop it as blind.
    frames = {"": [ax("combobox", "", node_id=50, value="Choose an option"),
                   ax("button", "Add to basket", node_id=51)]}
    surface = CdpConnectedSurface(FakeCdpTarget(frames))
    view = await surface.perceive()
    combos = [a for a in view.affordances if a.role == "combobox"]
    assert len(combos) == 1
    assert combos[0].value == "Choose an option"
    assert not view.blind


async def test_satisfier_sets_an_unnamed_select_end_to_end() -> None:
    # The full #110 regression: a nameless required-by-JS variant select is
    # perceived AND satisfied over the connected surface (0 before this fix).
    from zu_tools.selection import FirstOptionSelectionSatisfier

    frames = {"": [ax("combobox", "", node_id=50, value="Choose an option")]}
    target = FakeCdpTarget(frames)

    def choose_black(t: FakeCdpTarget, _params: dict) -> None:
        t.frames[""][0]["value"] = {"value": "Black"}
    target.effects[50] = choose_black
    surface = CdpConnectedSurface(target)

    results = await FirstOptionSelectionSatisfier().satisfy_required(surface)

    assert [r.chosen_label for r in results] == ["Black"]
    assert ("DOM.resolveNode", {"backendNodeId": 50}) in target.calls


class FakeOopifTarget:
    """A CDP endpoint with a page target + one CROSS-ORIGIN iframe target (a separate
    session), plus one ad-iframe target that must be skipped (#126). Models
    Target.getTargets / attachToTarget and session-routed AX/resolve/callFn."""

    _SESSION = "sess-iframe-1"

    def __init__(self, page_nodes: list[dict], iframe_nodes: list[dict],
                 *, iframe_url: str = "https://cmp.example/booking",
                 page_bounds: dict[int, list[float]] | None = None,
                 iframe_bounds: dict[int, list[float]] | None = None) -> None:
        self.page_nodes = page_nodes
        self.iframe_nodes = iframe_nodes
        self.iframe_url = iframe_url
        # Per-session geometry: the OOPIF's boxes only resolve in ITS session, so the
        # surface must capture the snapshot there — proven by keying these separately.
        self.page_bounds = page_bounds
        self.iframe_bounds = iframe_bounds
        self.calls: list[tuple[str, dict, str | None]] = []
        self.effects: dict[tuple[str | None, int], Any] = {}
        self._obj: dict[str, tuple[str | None, Any]] = {}

    async def send(self, method: str, params: dict | None = None,
                   *, session_id: str | None = None) -> dict:
        params = params or {}
        self.calls.append((method, params, session_id))
        if method == "Page.getFrameTree":
            return {}
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"title": "Book", "url": "https://salon.example"}}
        if method == "Target.getTargets":
            return {"targetInfos": [
                {"type": "iframe", "targetId": "tid-1", "url": self.iframe_url},
                {"type": "iframe", "targetId": "ad-1", "url": "https://doubleclick.net/pixel"},
                {"type": "page", "targetId": "p-0", "url": "https://salon.example"},
            ]}
        if method == "Target.attachToTarget":
            return {"sessionId": self._SESSION} if params.get("targetId") == "tid-1" else {}
        if method == "Accessibility.getFullAXTree":
            return {"nodes": list(self.iframe_nodes if session_id == self._SESSION else self.page_nodes)}
        if method == "DOMSnapshot.captureSnapshot":
            b = self.iframe_bounds if session_id == self._SESSION else self.page_bounds
            return _snapshot(b) if b is not None else {}
        if method == "DOM.resolveNode":
            oid = f"obj-{session_id}-{params.get('backendNodeId')}"
            self._obj[oid] = (session_id, params.get("backendNodeId"))
            return {"object": {"objectId": oid}}
        if method == "Runtime.callFunctionOn":
            key = self._obj.get(params.get("objectId", ""))
            effect = self.effects.get(key) if key is not None else None
            if effect is not None:
                effect(self)
            return {"result": {"value": None}}
        return {}


class NoSessionTarget:
    """A transport predating OOPIF support — its ``send`` has no ``session_id`` kwarg,
    so session-routed calls raise TypeError; the surface must skip OOPIFs gracefully."""

    def __init__(self, page_nodes: list[dict]) -> None:
        self.page_nodes = page_nodes

    async def send(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        if method == "Accessibility.getFullAXTree":
            return {"nodes": list(self.page_nodes)}
        if method == "Target.getTargets":
            return {"targetInfos": [{"type": "iframe", "targetId": "t", "url": "https://x.example"}]}
        if method == "Target.attachToTarget":
            return {"sessionId": "s"}
        return {}


async def test_perceive_includes_cross_origin_iframe_target_controls() -> None:
    # The failing case (#126): the site chrome is a 'Log in' link; the real booking UI
    # is a cross-origin widget in a separate iframe TARGET, invisible to the page tree.
    target = FakeOopifTarget(
        page_nodes=[ax("link", "Log in", node_id=1)],
        iframe_nodes=[ax("button", "Book 9:30", node_id=50)],
    )
    view = await CdpConnectedSurface(target).perceive()
    assert _labels(view) == ["Log in", "Book 9:30"]  # the OOPIF control is now present
    # the ad iframe was never attached; the real one was
    attached = [p.get("targetId") for m, p, _ in target.calls if m == "Target.attachToTarget"]
    assert "tid-1" in attached and "ad-1" not in attached


async def test_perceive_prunes_invisible_iframe_control_via_its_own_session_geometry() -> None:
    # Geometry for an OOPIF control must be captured in the iframe's OWN session (a
    # separate CDP target). Here the iframe carries a real 'Book 9:30' and a 0x0 hidden
    # 'Book 8:00'; the snapshot served in the iframe session prunes only the hidden one.
    target = FakeOopifTarget(
        page_nodes=[ax("link", "Log in", node_id=1)],
        iframe_nodes=[ax("button", "Book 9:30", node_id=50), ax("button", "Book 8:00", node_id=51)],
        page_bounds={1: [0.0, 0.0, 80.0, 20.0]},
        iframe_bounds={50: [0.0, 200.0, 100.0, 40.0], 51: [0.0, 0.0, 0.0, 0.0]},
    )
    view = await CdpConnectedSurface(target).perceive()
    assert _labels(view) == ["Log in", "Book 9:30"]  # hidden 'Book 8:00' pruned
    # The snapshot for the iframe control was captured in the iframe's session, not root.
    snap_sessions = [s for m, _, s in target.calls if m == "DOMSnapshot.captureSnapshot"]
    assert FakeOopifTarget._SESSION in snap_sessions and None in snap_sessions


async def test_act_on_a_cross_origin_iframe_control_routes_to_its_session() -> None:
    target = FakeOopifTarget(
        page_nodes=[ax("link", "Home", node_id=1)],
        iframe_nodes=[ax("button", "Book 9:30", node_id=50)],
    )
    def clear(t: FakeOopifTarget) -> None:
        t.iframe_nodes[:] = []
    target.effects[(FakeOopifTarget._SESSION, 50)] = clear
    surface = CdpConnectedSurface(target)
    view = await surface.perceive()
    book = next(a for a in view.affordances if a.label == "Book 9:30")

    after = await surface.act(SurfaceAction(handle=book.handle, kind="click"))

    # resolveNode + callFunctionOn were routed to the iframe's session, and the click took.
    assert ("DOM.resolveNode", {"backendNodeId": 50}, FakeOopifTarget._SESSION) in target.calls
    assert any(m == "Runtime.callFunctionOn" and s == FakeOopifTarget._SESSION
               for m, _, s in target.calls)
    assert "Book 9:30" not in _labels(after)


async def test_oopif_degrades_gracefully_when_transport_lacks_session_routing() -> None:
    # A transport whose send() predates OOPIF has no session_id kwarg — deliberately
    # NOT structurally a CdpTarget; the surface must still work over it.
    surface = CdpConnectedSurface(NoSessionTarget([ax("button", "Buy", node_id=1)]))  # type: ignore[arg-type]
    view = await surface.perceive()  # must not crash on the session-routed AX call
    assert _labels(view) == ["Buy"]  # page path intact; OOPIF simply skipped
