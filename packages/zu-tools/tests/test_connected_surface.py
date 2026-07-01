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


class FakeCdpTarget:
    """A minimal in-memory CDP endpoint. ``frames`` maps a frame id ("" = main) to
    its current AX node list; ``effects`` maps a backend node id to a callback that
    mutates ``frames`` when that node is acted on — the DOM effect a browser would
    apply, modelled at the AX level."""

    def __init__(self, frames: dict[str, list[dict]], *, title: str = "", url: str = "",
                 frame_tree: dict | None = None) -> None:
        self.frames = frames
        self.title = title
        self.url = url
        self.frame_tree = frame_tree
        self.calls: list[tuple[str, dict]] = []
        self.effects: dict[int, Any] = {}
        self._obj_to_node: dict[str, Any] = {}

    async def send(self, method: str, params: dict | None = None) -> dict:
        params = params or {}
        self.calls.append((method, params))
        if method == "Page.getFrameTree":
            return {"frameTree": self.frame_tree} if self.frame_tree else {}
        if method == "Accessibility.getFullAXTree":
            return {"nodes": list(self.frames.get(params.get("frameId", ""), []))}
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
