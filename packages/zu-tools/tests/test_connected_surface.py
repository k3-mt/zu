"""#93 — CdpConnectedSurface over a fake external CDP target, fully offline ($0).

No browser: a ``FakeCdpTarget`` answers the handful of CDP methods the surface
uses (``Page.getFrameTree``, ``Accessibility.getFullAXTree``,
``Target.getTargetInfo``, ``DOM.resolveNode``, ``Runtime.callFunctionOn``,
``Runtime.evaluate``, ``DOM.getBoxModel``, ``Input.dispatchMouseEvent``) from an
in-memory, mutable set of AX-tree snapshots — so ``perceive → act → perceive``
loops run for real. This mirrors ``test_action_surface``'s fake-session pattern.

The occlusion pass is scripted the same way: ``overlay`` answers the ONE
``Runtime.evaluate`` pre-probe; ``occlusion`` maps a backend node id to the
per-node probe VERDICT ('covered'/'clear'/'offscreen'/…, or an Exception to
raise). The trusted click is scripted via ``box_models`` (backend node id →
content quad); a dispatched ``mouseReleased`` at a quad's centre fires that
node's act effect — real input hits whatever owns the point.
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


class FakeCdpTarget:
    """A minimal in-memory CDP endpoint. ``frames`` maps a frame id ("" = main) to
    its current AX node list; ``effects`` maps a backend node id to a callback that
    mutates ``frames`` when that node is acted on — the DOM effect a browser would
    apply, modelled at the AX level. An effect fires on a CLICK-like call only: a
    trusted ``mouseReleased`` at the node's box centre, or a synthetic function that
    clicks/dispatches events — never on the scroll step or an occlusion probe."""

    def __init__(self, frames: dict[str, list[dict]], *, title: str = "", url: str = "",
                 frame_tree: dict | None = None) -> None:
        self.frames = frames
        self.title = title
        self.url = url
        self.frame_tree = frame_tree
        self.calls: list[tuple[str, dict]] = []
        self.effects: dict[int, Any] = {}
        self.overlay = False                      # the Runtime.evaluate pre-probe answer
        self.occlusion: dict[int, Any] = {}       # bid -> probe verdict (or Exception)
        self.box_models: dict[int, list[float]] = {}  # bid -> content quad (8 numbers)
        self.dispatch_error: Exception | None = None  # raised by Input.dispatchMouseEvent
        self.layout_viewport: dict | None = None  # Page.getLayoutMetrics cssLayoutViewport
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
        if method == "Target.getTargetInfo":
            return {"targetInfo": {"title": self.title, "url": self.url}}
        if method == "Runtime.evaluate":
            return {"result": {"value": self.overlay}}
        if method == "DOM.getBoxModel":
            box_bid = params.get("backendNodeId")
            quad = self.box_models.get(box_bid) if isinstance(box_bid, int) else None
            return {"model": {"content": list(quad)}} if quad else {}
        if method == "Page.getLayoutMetrics":
            return {"cssLayoutViewport": self.layout_viewport} if self.layout_viewport else {}
        if method == "Input.dispatchMouseEvent":
            if self.dispatch_error is not None:
                raise self.dispatch_error
            if params.get("type") == "mouseReleased":
                for hit_bid, hit_quad in self.box_models.items():
                    cx, cy = sum(hit_quad[0::2]) / 4.0, sum(hit_quad[1::2]) / 4.0
                    if abs(cx - params.get("x", -1)) < 0.5 and abs(cy - params.get("y", -1)) < 0.5:
                        effect = self.effects.get(hit_bid)
                        if effect is not None:
                            effect(self, params)
            return {}
        if method == "DOM.resolveNode":
            bid = params.get("backendNodeId")
            oid = f"obj-{bid}"
            self._obj_to_node[oid] = bid
            return {"object": {"objectId": oid}}
        if method == "Runtime.callFunctionOn":
            bid = self._obj_to_node.get(params.get("objectId", ""))
            decl = params.get("functionDeclaration", "")
            if "elementFromPoint" in decl:  # the occlusion probe — scripted verdict
                verdict = self.occlusion.get(bid, "clear") if isinstance(bid, int) else "clear"
                if isinstance(verdict, Exception):
                    raise verdict
                return {"result": {"value": verdict}}
            if "this.click()" in decl or "dispatchEvent" in decl:  # click/type/select/submit
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


async def test_perceive_falls_back_to_main_frame_when_no_frame_tree() -> None:
    frames = {"": [ax("button", "Buy", node_id=1)]}
    surface = CdpConnectedSurface(FakeCdpTarget(frames))
    view = await surface.perceive()
    assert _labels(view) == ["Buy"]


async def test_act_click_dispatches_trusted_input_at_the_box_centre() -> None:
    # The click contract (A9): REAL input first. The handle resolves to its GLOBAL
    # backend node id, the element is scrolled into view, and a trusted
    # mousePressed+mouseReleased (isTrusted:true in a real browser) lands at the
    # box-model centre — the synthetic this.click() is NOT used.
    frames = {"": [ax("button", "Accept all", node_id=10), ax("button", "Buy", node_id=11)]}
    target = FakeCdpTarget(frames)
    target.effects[10] = target._remove(10)  # clicking Accept removes it
    target.box_models[10] = [10, 20, 110, 20, 110, 60, 10, 60]  # centre (60, 40)
    surface = CdpConnectedSurface(target)

    view = await surface.perceive()
    accept = next(a for a in view.affordances if a.label == "Accept all")
    after = await surface.act(SurfaceAction(handle=accept.handle, kind="click"))

    assert _labels(after) == ["Buy"]  # accept cleared by the TRUSTED click, re-perceived
    assert ("DOM.resolveNode", {"backendNodeId": 10}) in target.calls
    assert ("DOM.getBoxModel", {"backendNodeId": 10}) in target.calls
    mouse = [p for m, p in target.calls if m == "Input.dispatchMouseEvent"]
    assert [p["type"] for p in mouse] == ["mousePressed", "mouseReleased"]
    assert all(p["button"] == "left" and p["clickCount"] == 1 for p in mouse)
    assert mouse[0]["x"] == 60 and mouse[0]["y"] == 40  # the box centre, not (0,0)
    assert not any(
        "this.click()" in p.get("functionDeclaration", "")
        for m, p in target.calls if m == "Runtime.callFunctionOn"
    )


async def test_act_click_falls_back_to_synthetic_without_box_model() -> None:
    # No box model (detached / a transport without the DOM box-model op): the click
    # falls back to the synthetic this.click() arm and still takes effect.
    frames = {"": [ax("button", "Accept all", node_id=10), ax("button", "Buy", node_id=11)]}
    target = FakeCdpTarget(frames)
    target.effects[10] = target._remove(10)
    surface = CdpConnectedSurface(target)

    view = await surface.perceive()
    accept = next(a for a in view.affordances if a.label == "Accept all")
    after = await surface.act(SurfaceAction(handle=accept.handle, kind="click"))

    assert _labels(after) == ["Buy"]  # the synthetic fallback clicked it
    assert not any(m == "Input.dispatchMouseEvent" for m, _ in target.calls)
    assert any(
        "this.click()" in p.get("functionDeclaration", "")
        for m, p in target.calls if m == "Runtime.callFunctionOn"
    )


async def test_act_click_falls_back_on_zero_area_box_and_dispatch_raise() -> None:
    # A degenerate (zero-area) box model is not trusted-clickable — synthetic arm.
    frames = {"": [ax("button", "Go", node_id=10)]}
    target = FakeCdpTarget(frames)
    target.box_models[10] = [5, 5, 5, 5, 5, 5, 5, 5]
    clicked: list[str] = []
    target.effects[10] = lambda t, p: clicked.append("go")
    surface = CdpConnectedSurface(target)
    view = await surface.perceive()
    await surface.act(SurfaceAction(handle=view.affordances[0].handle, kind="click"))
    assert clicked == ["go"]  # via the synthetic arm
    assert not any(m == "Input.dispatchMouseEvent" for m, _ in target.calls)

    # And a dispatch that RAISES (transport without the Input domain) also falls back.
    target2 = FakeCdpTarget({"": [ax("button", "Go", node_id=10)]})
    target2.box_models[10] = [0, 0, 10, 0, 10, 10, 0, 10]
    target2.dispatch_error = RuntimeError("Input domain unavailable")
    clicked2: list[str] = []
    target2.effects[10] = lambda t, p: clicked2.append("go")
    surface2 = CdpConnectedSurface(target2)
    view2 = await surface2.perceive()
    await surface2.act(SurfaceAction(handle=view2.affordances[0].handle, kind="click"))
    assert clicked2 == ["go"]  # the raise was swallowed; the synthetic arm clicked


async def test_act_click_falls_back_when_the_box_centre_is_below_the_viewport() -> None:
    # F2: on a scroll-behavior:smooth site the scroll animates async, so getBoxModel can read a
    # PRE-scroll box whose centre is below the fold. Dispatching a trusted press there hits empty
    # space and the widget never fires. The in-viewport bound must reject it and fall back to the
    # synthetic this.click(), which targets the node itself regardless of where it sits.
    frames = {"": [ax("button", "Continue", node_id=10)]}
    target = FakeCdpTarget(frames)
    target.layout_viewport = {"clientWidth": 1280, "clientHeight": 720}
    target.box_models[10] = [100, 3100, 300, 3100, 300, 3160, 100, 3160]  # centre y=3130, below
    clicked: list[str] = []
    target.effects[10] = lambda t, p: clicked.append("continue")
    surface = CdpConnectedSurface(target)
    view = await surface.perceive()
    await surface.act(SurfaceAction(handle=view.affordances[0].handle, kind="click"))
    assert clicked == ["continue"]  # the synthetic arm hit the node
    assert not any(m == "Input.dispatchMouseEvent" for m, _ in target.calls)  # no blind press


def test_scroll_step_is_instant_not_smooth() -> None:
    # F2 root cause: an async (smooth) scroll is what leaves the box stale. The scroll step must
    # request an INSTANT scroll so the box is settled before getBoxModel reads it.
    from zu_tools.connected_surface import _SCROLL_FN
    assert "behavior:'instant'" in _SCROLL_FN.replace(" ", "")


def test_covered_probe_clears_a_control_hit_tested_to_its_own_label() -> None:
    # F1: the styled-label radio/checkbox pattern (a visually-hidden <input> whose clickable
    # proxy is its own <label> — Bootstrap/Tailwind/MUI) hit-tests to that label, a sibling.
    # The occlusion probe must read that as the control's OWN affordance (clear), not occlusion,
    # or it prunes the modal's own slot/variant grid. The JS logic is browser-only; pin it here.
    from zu_tools.connected_surface import _COVERED_FN
    src = _COVERED_FN.replace(" ", "")
    assert "this.labels" in src and "hl.control===this" in src


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


# --- the occlusion pass (A7-class overlays) ----------------------------------


async def test_no_overlay_means_zero_occlusion_probe_traffic() -> None:
    # The common path: interactive candidates exist, the ONE Runtime.evaluate
    # pre-probe says no overlay — the pass ends there, no per-node CDP traffic.
    frames = {"": [ax("button", "Buy", node_id=1), ax("button", "Basket", node_id=2)]}
    target = FakeCdpTarget(frames)  # overlay=False

    view = await CdpConnectedSurface(target).perceive()

    assert _labels(view) == ["Buy", "Basket"]
    assert view.covered_count == 0
    methods = [m for m, _ in target.calls]
    assert methods.count("Runtime.evaluate") == 1      # the one cheap pre-probe
    assert "DOM.resolveNode" not in methods            # no per-node probes at all
    assert "Runtime.callFunctionOn" not in methods

    # And with NOTHING interactive to protect, even the pre-probe is skipped.
    empty = FakeCdpTarget({"": [ax("heading", "About us")]})
    await CdpConnectedSurface(empty).perceive()
    assert "Runtime.evaluate" not in [m for m, _ in empty.calls]


async def test_overlay_prunes_covered_nodes_and_choose_one_skips_them() -> None:
    # The A7 punch-through, closed end-to-end: a full-viewport overlay is up; the
    # page-behind slot grid is covered, the overlay's own controls are not. The
    # covered slots never become affordances, so choose_one can only ever pick an
    # overlay control — the fingerprint verify has nothing covered to bless.
    from zu_tools.choose import ChooseOne

    frames = {"": [
        ax("button", "9:00", node_id=1), ax("button", "9:30", node_id=2),
        ax("button", "Accept all", node_id=10), ax("button", "Manage choices", node_id=11),
    ]}
    target = FakeCdpTarget(frames)
    target.overlay = True
    target.occlusion = {1: "covered", 2: "covered", 10: "clear", 11: "clear"}
    surface = CdpConnectedSurface(target)

    view = await surface.perceive()
    assert _labels(view) == ["Accept all", "Manage choices"]  # covered slots pruned
    assert view.covered_count == 2
    assert not view.blind  # the overlay's own controls ARE the actionable surface

    clicked: list[int] = []
    for bid in (1, 2, 10, 11):
        target.effects[bid] = (lambda b: lambda t, p: clicked.append(b))(bid)
    await ChooseOne().apply(surface, hint="first")
    assert clicked == [10]  # the overlay's first control; never a covered slot


async def test_all_covered_reads_occluded_not_a_healthy_empty_page() -> None:
    # An overlay with no AX-visible controls of its own swallowed the page: the
    # view must degrade LOUDLY (blind, with an occlusion reason + count), never
    # read as a healthy empty page.
    frames = {"": [ax("button", "9:00", node_id=1), ax("button", "9:30", node_id=2)]}
    target = FakeCdpTarget(frames)
    target.overlay = True
    target.occlusion = {1: "covered", 2: "covered"}

    view = await CdpConnectedSurface(target).perceive()

    assert view.affordances == ()
    assert view.covered_count == 2
    assert view.blind
    assert view.blind_reason is not None and "covered" in view.blind_reason


async def test_below_viewport_node_under_overlay_is_not_covered() -> None:
    # An unscrolled-to element probes 'offscreen' — actionable via scrollIntoView,
    # so it is KEPT (the probe point is never clamped into the viewport; clamping
    # is exactly the downstream bug this contract exists to avoid).
    frames = {"": [ax("button", "Late slot", node_id=1), ax("button", "9:00", node_id=2)]}
    target = FakeCdpTarget(frames)
    target.overlay = True
    target.occlusion = {1: "offscreen", 2: "covered"}

    view = await CdpConnectedSurface(target).perceive()

    assert _labels(view) == ["Late slot"]
    assert view.covered_count == 1


async def test_occlusion_probe_error_keeps_the_node() -> None:
    # Fail-open: a probe that raises must never hide a control.
    frames = {"": [ax("button", "Buy", node_id=1), ax("button", "9:00", node_id=2)]}
    target = FakeCdpTarget(frames)
    target.overlay = True
    target.occlusion = {1: RuntimeError("probe blew up"), 2: "covered"}

    view = await CdpConnectedSurface(target).perceive()

    assert _labels(view) == ["Buy"]  # kept despite the probe error
    assert view.covered_count == 1


class FakeOopifTarget:
    """A CDP endpoint with a page target + one CROSS-ORIGIN iframe target (a separate
    session), plus one ad-iframe target that must be skipped (#126). Models
    Target.getTargets / attachToTarget and session-routed AX/resolve/callFn."""

    _SESSION = "sess-iframe-1"

    def __init__(self, page_nodes: list[dict], iframe_nodes: list[dict],
                 *, iframe_url: str = "https://cmp.example/booking") -> None:
        self.page_nodes = page_nodes
        self.iframe_nodes = iframe_nodes
        self.iframe_url = iframe_url
        self.calls: list[tuple[str, dict, str | None]] = []
        self.effects: dict[tuple[str | None, int], Any] = {}
        self.overlay = False                  # the Runtime.evaluate pre-probe answer
        self.occlusion: dict[int, str] = {}   # root-session bid -> probe verdict
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
        if method == "Runtime.evaluate":
            return {"result": {"value": self.overlay}}
        if method == "DOM.resolveNode":
            oid = f"obj-{session_id}-{params.get('backendNodeId')}"
            self._obj[oid] = (session_id, params.get("backendNodeId"))
            return {"object": {"objectId": oid}}
        if method == "Runtime.callFunctionOn":
            key = self._obj.get(params.get("objectId", ""))
            decl = params.get("functionDeclaration", "")
            if "elementFromPoint" in decl:  # the occlusion probe — scripted verdict
                return {"result": {"value": self.occlusion.get(key[1] if key else -1, "clear")}}
            if "this.click()" in decl or "dispatchEvent" in decl:
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


async def test_occlusion_probe_skips_oopif_nodes() -> None:
    # Flat session routing is not portable across transports (a Playwright CDPSession
    # rejects session_id), so the occlusion pass probes ROOT-session nodes only: the
    # OOPIF's control is never probed (and never pruned), even with the overlay up.
    target = FakeOopifTarget(
        page_nodes=[ax("button", "Page btn A", node_id=1), ax("button", "Page btn B", node_id=2)],
        iframe_nodes=[ax("button", "Book 9:30", node_id=50)],
    )
    target.overlay = True
    target.occlusion = {1: "covered", 2: "covered", 50: "covered"}  # 50 must never be asked

    view = await CdpConnectedSurface(target).perceive()

    assert _labels(view) == ["Book 9:30"]  # page controls pruned; OOPIF control kept
    assert view.covered_count == 2
    # No occlusion probe was routed to the iframe session.
    assert not any(
        m == "DOM.resolveNode" and s is not None for m, _p, s in target.calls
    )
