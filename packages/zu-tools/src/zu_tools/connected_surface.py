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

from .action_surface import AxNode, normalize_axtree, reduce_surface
from .surface_adapter import to_surface_view

# The shipped verbs ``act()`` resolves. ``kind`` on a SurfaceAction is a free
# string; anything else falls through to a click (the safe default) — an unknown
# verb is never a crash.
_TYPE = "type"
_SELECT = "select"
_SUBMIT = "submit"

# Roles that are actionable by their own structure/options rather than an
# accessible name — a native <select> (combobox) or an ARIA listbox. A variant
# picker routinely has no accessible name, and we resolve it by backend node id,
# so we keep it in the surface instead of dropping it as blind (#110).
_SELF_ADDRESSING_ROLES: frozenset[str] = frozenset({"combobox", "listbox"})

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

# Submit a field the way a keyboard would: focus it, fire an Enter key sequence
# (keydown/keypress/keyup), and — if it lives in a form — requestSubmit()/submit(). This
# is what a 'search on Enter' box needs when there is no visible submit button; it is the
# ``submit`` verb the ``search`` primitive issues. Content-free — it presses Enter, never
# reads the field.
_SUBMIT_FN = (
    "function(){ this.focus();"
    " const ev = function(t){ return new KeyboardEvent(t, {key:'Enter', code:'Enter',"
    " keyCode:13, which:13, bubbles:true, cancelable:true}); };"
    " this.dispatchEvent(ev('keydown')); this.dispatchEvent(ev('keypress'));"
    " this.dispatchEvent(ev('keyup'));"
    " const f = this.form || (this.closest ? this.closest('form') : null);"
    " if (f) { if (f.requestSubmit) { f.requestSubmit(); } else { f.submit(); } } }"
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


# Bound the cross-origin iframe targets we attach to per perceive (#126) — a page
# can embed dozens of ad/tracker frames; we take the first few real ones only.
_MAX_IFRAME_TARGETS = 8
# Hosts whose iframes are ads / trackers / analytics — skipped so their noise never
# enters the surface. A content-free URL-host heuristic (never page text).
_AD_FRAME_HOST_MARKERS: tuple[str, ...] = (
    "doubleclick", "googlesyndication", "googletagmanager", "google-analytics",
    "googleadservices", "adservice", "/ads/", "adsystem", "amazon-adsystem",
    "facebook.com/tr", "connect.facebook", "hotjar", "segment.io", "segment.com",
    "amplitude", "mixpanel", "criteo", "taboola", "outbrain", "scorecardresearch",
    "quantserve", "moatads", "adnxs", "casalemedia",
)


def _is_ad_frame(url: str) -> bool:
    low = url.lower()
    return any(m in low for m in _AD_FRAME_HOST_MARKERS)


@runtime_checkable
class CdpTarget(Protocol):
    """The one method a host's CDP connection must expose: send a Chrome DevTools
    Protocol command and await its JSON result — exactly a raw devtools client or
    Playwright's ``CDPSession.send(method, params)``. Kept minimal + injectable so
    the host wires its OWN browser in (Zu launches nothing) and so the surface is
    tested over a fake transport at $0.

    ``session_id`` is the FLAT-protocol routing field: when the surface attaches to a
    CROSS-ORIGIN iframe target (an OOPIF, #126) it passes that target's session id so
    the command runs in that target. A transport that does not accept ``session_id``
    simply does not support OOPIFs — the surface degrades gracefully (it skips them),
    so the base ``send(method, params)`` contract is unchanged for existing hosts."""

    async def send(
        self, method: str, params: dict | None = None, *, session_id: str | None = None
    ) -> dict: ...


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
        # The page's own tree (root session) PLUS each cross-origin iframe target
        # (an OOPIF is a separate CDP target the page tree cannot see, #126). Each
        # source is normalised on its own so its group/enclosing structure is
        # per-tree, and iframe nodes carry their session id for the act path.
        ax_nodes = await self._page_ax_nodes()
        ax_nodes.extend(await self._iframe_ax_nodes())
        title, url = await self._title_url()
        surface = reduce_surface(
            ax_nodes, title=title, url=url,
            unlabeled_ratio=self._unlabeled_ratio,
            # A ConnectedSurface resolves handles by GLOBAL backend node id, so a
            # self-addressing control (a <select> variant picker) is actionable even
            # with no accessible name — keep it rather than drop it as blind (#110).
            keep_unnamed_roles=_SELF_ADDRESSING_ROLES,
        )
        self._handle_map = dict(surface.handle_map)
        return to_surface_view(surface)

    async def _page_ax_nodes(self) -> list[AxNode]:
        """The page target's AX nodes (root session): the main frame + same-origin
        child frames, de-duplicated by global backend node id."""
        seen: set[int] = set()
        raw: list[dict] = []
        for frame_id in await self._frame_ids():
            params: dict[str, Any] = {"frameId": frame_id} if frame_id else {}
            resp = await self._send("Accessibility.getFullAXTree", params)
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
                raw.append(n)
        return normalize_axtree(raw)

    async def _iframe_ax_nodes(self) -> list[AxNode]:
        """The AX nodes of each CROSS-ORIGIN iframe target (#126): attach to the
        target, pull its full AX tree in that session, and stamp its session id so
        the act path routes ``DOM.resolveNode`` / ``callFunctionOn`` back to it."""
        out: list[AxNode] = []
        for target_id in await self._iframe_targets():
            session_id = await self._attach(target_id)
            if session_id is None:
                continue  # transport can't attach / doesn't route sessions — skip it
            resp = await self._send("Accessibility.getFullAXTree", {}, session_id=session_id)
            nodes = resp.get("nodes") if isinstance(resp, dict) else None
            if not isinstance(nodes, list):
                continue
            out.extend(
                normalize_axtree(
                    [n for n in nodes if isinstance(n, dict)], session_id=session_id
                )
            )
        return out

    async def act(self, action: SurfaceAction) -> SurfaceView:
        object_id, session_id = await self._resolve(action.handle)
        if object_id is not None:
            if action.kind == _TYPE:
                await self._call_fn(object_id, _TYPE_FN, [{"value": action.text or ""}], session_id)
            elif action.kind == _SELECT:
                await self._call_fn(object_id, _SELECT_FN, [{"value": action.text}], session_id)
            elif action.kind == _SUBMIT:
                await self._call_fn(object_id, _SUBMIT_FN, session_id=session_id)
            else:  # click — the default verb
                await self._call_fn(object_id, _CLICK_FN, session_id=session_id)
        # A stale/unresolvable handle is an escalation, not a crash (§11.3): we
        # simply re-perceive; the caller sees the handle gone and re-captures.
        return await self.perceive()

    # --- CDP plumbing --------------------------------------------------------

    async def _send(
        self, method: str, params: dict | None = None, session_id: str | None = None
    ) -> dict:
        """One send, optionally routed to an OOPIF session (#126). A transport that
        does not accept ``session_id`` raises ``TypeError`` — we swallow it and return
        empty, so an OOPIF is simply skipped rather than crashing the page path."""
        if session_id is None:
            return await self._target.send(method, params or {})
        try:
            return await self._target.send(method, params or {}, session_id=session_id)
        except TypeError:
            return {}

    async def _iframe_targets(self) -> list[str]:
        """The cross-origin iframe target ids to attach to (#126): CDP targets of type
        'iframe', ad/tracker hosts skipped, bounded to :data:`_MAX_IFRAME_TARGETS`."""
        resp = await self._send("Target.getTargets")
        infos = resp.get("targetInfos") if isinstance(resp, dict) else None
        if not isinstance(infos, list):
            return []
        out: list[str] = []
        for t in infos:
            if not isinstance(t, dict) or t.get("type") != "iframe":
                continue
            if _is_ad_frame(str(t.get("url", ""))):
                continue
            tid = t.get("targetId")
            if isinstance(tid, str):
                out.append(tid)
            if len(out) >= _MAX_IFRAME_TARGETS:
                break
        return out

    async def _attach(self, target_id: str) -> str | None:
        """Attach to a child target with ``flatten`` and return its session id."""
        resp = await self._send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        sid = resp.get("sessionId") if isinstance(resp, dict) else None
        return sid if isinstance(sid, str) else None

    async def _frame_ids(self) -> list[str]:
        """``""`` (the main frame, no frameId — flattens its shadow roots + same-doc
        iframes) plus each same-origin child frame id. Falls back to just the main
        frame when the target exposes no frame tree."""
        resp = await self._send("Page.getFrameTree", {})
        tree = resp.get("frameTree") if isinstance(resp, dict) else None
        ids: list[str] = [""]
        if isinstance(tree, dict):
            _collect_child_frames(tree, ids)
        return ids

    async def _title_url(self) -> tuple[str, str]:
        resp = await self._send("Target.getTargetInfo", {})
        info = resp.get("targetInfo") if isinstance(resp, dict) else None
        if isinstance(info, dict):
            return str(info.get("title", "")), str(info.get("url", ""))
        return "", ""

    async def _resolve(self, handle: str) -> tuple[str | None, str | None]:
        """Resolve an opaque handle to a live JS object id + its session, ACROSS
        boundaries: the handle map carries the element's GLOBAL backend DOM-node id
        (which ``DOM.resolveNode`` turns into an objectId regardless of shadow root /
        frame) and, for an OOPIF control, the session it lives in (#126). ``(None,
        None)`` (unknown handle / no node id) is a re-capture signal."""
        locator = self._handle_map.get(handle)
        node_id = locator.get("node_id") if isinstance(locator, dict) else None
        session_id = locator.get("session_id") if isinstance(locator, dict) else None
        session_id = session_id if isinstance(session_id, str) else None
        if not isinstance(node_id, int):
            return None, None
        resp = await self._send("DOM.resolveNode", {"backendNodeId": node_id}, session_id=session_id)
        obj = resp.get("object") if isinstance(resp, dict) else None
        object_id = obj.get("objectId") if isinstance(obj, dict) else None
        return (object_id if isinstance(object_id, str) else None), session_id

    async def _call_fn(
        self, object_id: str, declaration: str, args: list[dict] | None = None,
        session_id: str | None = None,
    ) -> dict:
        return await self._send(
            "Runtime.callFunctionOn",
            {
                "objectId": object_id,
                "functionDeclaration": declaration,
                "arguments": args or [],
                "returnByValue": True,
            },
            session_id=session_id,
        )
