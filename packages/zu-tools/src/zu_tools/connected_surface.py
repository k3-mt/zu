"""connected_surface — an Action Surface bound to an EXTERNAL CDP target (#93).

``zu_tools.action_surface`` already reduces a page to content-free,
handle-addressed affordances from the CDP accessibility tree — and that tree
flattens OPEN shadow roots and same-document iframes for free (a plain
``document.querySelectorAll`` does NOT cross shadow boundaries, so controls
inside web components / CMP widgets are invisible to a hand-rolled walk). But
that reducer + its act-by-handle were welded to Zu's own ``SessionBackend``.

:class:`CdpConnectedSurface` reuses the SAME reducer over a browser target a
HOST already owns — reached over an external CDP endpoint (e.g. a sandboxed
Chromium the host started and connected to via ``connect_over_cdp``). It is the
reference :class:`~zu_core.ports.ConnectedSurface`:

  * ``perceive()`` walks the target's frame tree, pulls each frame's full AX tree
    (open shadow roots + same-document iframes already flattened), concatenates
    the nodes in document order — de-duplicated by their GLOBAL backend DOM-node
    id so a same-origin child frame that already appears in the main tree is not
    double-counted — and runs Zu's deterministic reducer. The caller gets Zu's
    stable handles + blind detector, shadow/frame-flattened, from one call.
  * ``act()`` resolves the opaque handle to its backend DOM node (an id that is
    global to the target — the SAME id regardless of which shadow root or frame
    the element lives in, which is exactly what "resolve across a boundary"
    needs), performs the verb, and RE-PERCEIVES so the returned view reflects the
    effect (or, for a stale handle, shows it gone — an escalation, not a crash).

The transport is a tiny injectable :class:`CdpTarget` (one ``send`` method,
exactly a raw devtools client / Playwright's ``CDPSession.send``), so the surface
is driven — and TESTED — over a fake at $0 with no browser.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from zu_core.ports import SurfaceAction
from zu_core.surface import SurfaceView

from .action_surface import normalize_axtree, reduce_surface
from .surface_adapter import to_surface_view

# The shipped verbs ``act()`` resolves. ``kind`` on a SurfaceAction is a free
# string; anything else falls through to a click (the safe default) — an unknown
# verb is never a crash.
_TYPE = "type"
_SELECT = "select"

# A click that crosses shadow/frame boundaries: the element is resolved to a
# global objectId first, so ``this.click()`` fires inside whatever root it lives
# in. (The pointer tool handles the isTrusted/hover case separately, §12.)
_CLICK_FN = "function(){ this.scrollIntoView({block:'center'}); this.click(); }"

# Set a field's value through the native setter so React/Vue controlled inputs
# see the change, then fire input+change (what a real keystroke would).
_TYPE_FN = (
    "function(v){ this.focus();"
    " const d = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(this), 'value');"
    " if (d && d.set) { d.set.call(this, v); } else { this.value = v; }"
    " this.dispatchEvent(new Event('input', {bubbles: true}));"
    " this.dispatchEvent(new Event('change', {bubbles: true})); }"
)

# The deterministic option-picker (#95's mechanic, executed browser-side): choose
# the option whose label/text/value matches ``wanted``; when ``wanted`` is null,
# choose the FIRST VALID option — skipping placeholders (empty value) and disabled
# options — but only if the control is still UNSET (so it never overrides a choice
# that already took). Fires input+change so the shop re-prices / enables add-to-
# basket. Returns the chosen option's text (the caller reads it back off the
# re-perceived surface). Content-free: it chooses by option STRUCTURE, never prose.
_SELECT_FN = (
    "function(wanted){"
    " const opts = Array.prototype.slice.call(this.options || []);"
    " const valid = opts.filter(function(o){ return !o.disabled && o.value !== ''; });"
    " let chosen = null;"
    " if (wanted != null) {"
    "   chosen = valid.filter(function(o){"
    "     return o.label === wanted || o.text === wanted || o.value === wanted; })[0] || null;"
    " } else {"
    "   const cur = this.options[this.selectedIndex];"
    "   if (this.value !== '' && cur && !cur.disabled) { return null; }"  # already set — leave it
    "   chosen = valid[0] || null;"
    " }"
    " if (!chosen) { return null; }"
    " this.value = chosen.value;"
    " this.dispatchEvent(new Event('input', {bubbles: true}));"
    " this.dispatchEvent(new Event('change', {bubbles: true}));"
    " return chosen.text; }"
)


@runtime_checkable
class CdpTarget(Protocol):
    """The one method a host's CDP connection must expose: send a Chrome DevTools
    Protocol command and await its JSON result — exactly a raw devtools client or
    Playwright's ``CDPSession.send(method, params)``. Kept minimal + injectable so
    the host wires its OWN browser in (Zu launches nothing) and so the surface is
    tested over a fake transport at $0."""

    async def send(self, method: str, params: dict | None = None) -> dict: ...


def _collect_child_frames(node: dict, out: list[str]) -> None:
    """Depth-first collect every CHILD frame id under ``node`` (a ``Page.FrameTree``).
    The root frame is intentionally excluded — it is covered by the no-frameId
    ``getFullAXTree`` call, which already flattens its open shadow roots + same-doc
    iframes; only cross-origin child frames (OOPIFs) need their own pull."""
    for child in node.get("childFrames") or []:
        if not isinstance(child, dict):
            continue
        frame = child.get("frame")
        fid = frame.get("id") if isinstance(frame, dict) else None
        if isinstance(fid, str):
            out.append(fid)
        _collect_child_frames(child, out)


class CdpConnectedSurface:
    """The reference :class:`~zu_core.ports.ConnectedSurface` over a :class:`CdpTarget`."""

    __zu_interface__ = 1  # the connected_surfaces interface major this targets
    name = "cdp_connected_surface"

    def __init__(self, target: CdpTarget, *, unlabeled_ratio: float = 0.5) -> None:
        self._target = target
        self._unlabeled_ratio = unlabeled_ratio
        # handle -> {role, name, node_id?}. Harness-side, never model-visible —
        # exactly like action_surface's handle_map; ``act`` resolves against it.
        self._handle_map: dict[str, dict] = {}

    async def perceive(self) -> SurfaceView:
        seen: set[int] = set()
        nodes: list[dict] = []
        for frame_id in await self._frame_ids():
            params: dict[str, Any] = {"frameId": frame_id} if frame_id else {}
            resp = await self._target.send("Accessibility.getFullAXTree", params)
            frame_nodes = resp.get("nodes") if isinstance(resp, dict) else None
            if not isinstance(frame_nodes, list):
                continue
            for n in frame_nodes:
                if not isinstance(n, dict):
                    continue
                bid = n.get("backendDOMNodeId")
                if isinstance(bid, int):
                    if bid in seen:
                        continue  # already collected from the main tree / another frame
                    seen.add(bid)
                nodes.append(n)
        title, url = await self._title_url()
        surface = reduce_surface(
            normalize_axtree(nodes), title=title, url=url, unlabeled_ratio=self._unlabeled_ratio
        )
        self._handle_map = dict(surface.handle_map)
        return to_surface_view(surface)

    async def act(self, action: SurfaceAction) -> SurfaceView:
        object_id = await self._resolve(action.handle)
        if object_id is not None:
            if action.kind == _TYPE:
                await self._call_fn(object_id, _TYPE_FN, [{"value": action.text or ""}])
            elif action.kind == _SELECT:
                await self._call_fn(object_id, _SELECT_FN, [{"value": action.text}])
            else:  # click — the default verb
                await self._call_fn(object_id, _CLICK_FN)
        # A stale/unresolvable handle is an escalation, not a crash (§11.3): we
        # simply re-perceive; the caller sees the handle gone and re-captures.
        return await self.perceive()

    # --- CDP plumbing --------------------------------------------------------

    async def _frame_ids(self) -> list[str]:
        """``""`` (the main frame, no frameId — flattens its shadow roots + same-doc
        iframes) plus each cross-origin child frame id. Falls back to just the main
        frame when the target exposes no frame tree."""
        resp = await self._target.send("Page.getFrameTree", {})
        tree = resp.get("frameTree") if isinstance(resp, dict) else None
        ids: list[str] = [""]
        if isinstance(tree, dict):
            _collect_child_frames(tree, ids)
        return ids

    async def _title_url(self) -> tuple[str, str]:
        resp = await self._target.send("Target.getTargetInfo", {})
        info = resp.get("targetInfo") if isinstance(resp, dict) else None
        if isinstance(info, dict):
            return str(info.get("title", "")), str(info.get("url", ""))
        return "", ""

    async def _resolve(self, handle: str) -> str | None:
        """Resolve an opaque handle to a live JS object id, ACROSS boundaries: the
        handle map carries the element's GLOBAL backend DOM-node id, which
        ``DOM.resolveNode`` turns into an objectId regardless of the shadow root /
        frame it lives in. ``None`` (unknown handle, or a tree with no node id) is a
        re-capture signal, handled by ``act`` re-perceiving."""
        locator = self._handle_map.get(handle)
        node_id = locator.get("node_id") if isinstance(locator, dict) else None
        if not isinstance(node_id, int):
            return None
        resp = await self._target.send("DOM.resolveNode", {"backendNodeId": node_id})
        obj = resp.get("object") if isinstance(resp, dict) else None
        object_id = obj.get("objectId") if isinstance(obj, dict) else None
        return object_id if isinstance(object_id, str) else None

    async def _call_fn(
        self, object_id: str, declaration: str, args: list[dict] | None = None
    ) -> dict:
        return await self._target.send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": declaration,
                "arguments": args or [],
                "returnByValue": True,
            },
        )
