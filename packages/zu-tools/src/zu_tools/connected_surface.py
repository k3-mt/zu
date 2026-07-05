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
    An AX tree carries NO paint order, so before reducing, perceive() runs an
    OCCLUSION pass (geometry only, see ``_OVERLAY_PROBE_JS``): when a
    full-viewport overlay is up, page-behind controls are hit-tested and the
    covered ones pruned — so a covered control never becomes an affordance and a
    primitive can never "click through" the overlay. No overlay ⇒ the pass costs
    one ``Runtime.evaluate`` and nothing else.
  * ``act()`` resolves the opaque handle to its backend DOM node (an id that is
    global to the target — the SAME id regardless of which shadow root or frame
    the element lives in, which is exactly what "resolve across a boundary"
    needs), performs the verb, and RE-PERCEIVES so the returned view reflects the
    effect (or, for a stale handle, shows it gone — an escalation, not a crash).
    The click verb dispatches REAL trusted input first (``Input.dispatchMouseEvent``
    at the element's box-model centre — widgets that check ``isTrusted`` ignore a
    synthetic ``.click()``), falling back to the synthetic click when no box model
    is available.

The transport is a tiny injectable :class:`CdpTarget` (one ``send`` method,
exactly a raw devtools client / Playwright's ``CDPSession.send``), so the surface
is driven — and TESTED — over a fake at $0 with no browser.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from zu_core.ports import SurfaceAction
from zu_core.surface import SurfaceView

from .action_surface import INTERACTIVE_ROLES, AxNode, normalize_axtree, reduce_surface
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

# Bring the element into view before the trusted click reads its box model — the
# same centring scroll the synthetic click always did, as its own step.
# INSTANT, not smooth: a site with CSS ``scroll-behavior:smooth`` (Bootstrap sets it on :root
# for every page) animates ``scrollIntoView`` asynchronously, so the very next ``getBoxModel``
# reads the PRE-scroll box and the trusted press fires below the fold, on nothing. ``'instant'``
# lands the scroll before the box is read; the ``_trusted_click`` in-viewport bound is the belt.
_SCROLL_FN = "function(){ this.scrollIntoView({block:'center', behavior:'instant'}); }"

# The SYNTHETIC click FALLBACK, crossing shadow/frame boundaries: the element is
# resolved to a global objectId first, so ``this.click()`` fires inside whatever
# root it lives in. The click verb tries REAL trusted input first (``_trusted_click``:
# ``DOM.getBoxModel`` centre → ``Input.dispatchMouseEvent``, because widgets that
# check ``event.isTrusted`` ignore a synthetic ``.click()``); this fallback keeps
# every element clickable when no box model is available (detached/zero-area, a
# transport that lacks the Input domain).
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


# --- occlusion pass ----------------------------------------------------------
# ``Accessibility.getFullAXTree`` carries no z-order: a control UNDER a
# full-viewport overlay (a consent wall, an interstitial) is indistinguishable
# from an actionable one, so a consumer could click "through" the overlay and the
# fingerprint-change verify would bless the punch-through. perceive() closes that
# with GEOMETRY ONLY (element boxes, hit-testing, containment — never a URL, class
# name or any site-shaped constant):
#
#   1. ONE cheap ``Runtime.evaluate`` pre-probe asks "is a full-viewport overlay
#      up?" — the element stack at the viewport centre contains a fixed/sticky
#      element of stacking z-index >= _OVERLAY_MIN_Z covering >= ~80% x 60% of the
#      viewport (generic geometry thresholds for "an overlay", akin to the blind
#      detector's unlabeled_ratio). No overlay ⇒ the whole pass is skipped: ZERO
#      probe traffic on the common path.
#   2. Overlay up ⇒ each interactive candidate node (has a ``node_id``, lives in
#      the ROOT session — an OOPIF node is skipped because flat ``session_id``
#      routing is not portable across transports) is hit-tested ONCE
#      (``DOM.resolveNode`` + ``Runtime.callFunctionOn`` of ``_COVERED_FN``),
#      capped at :data:`_MAX_OCCLUSION_PROBES` probes.
#   3. ``_COVERED_FN`` verdicts: the box centre; a centre OUTSIDE the viewport is
#      NOT covered (an unscrolled-to element is actionable via scrollIntoView —
#      the probe point is deliberately never clamped to the viewport edge); else
#      ``elementFromPoint`` in the element's OWN root, covered iff a hit exists
#      and neither contains the other.
#
# Every failure fails OPEN (not covered): a broken probe must never hide a
# control — over-perception is an escalation, silent under-perception is not.
#
# The cap bounds probe traffic (it only spends when an overlay is actually up). Candidates are
# probed in DOCUMENT order — page-behind controls come first, the overlay's own controls last —
# so the cap only ever leaves LATE nodes unprobed-and-kept, which are the overlay's own on a
# normal modal. Set high enough that a link/nav/footer-dense page-behind (routinely >120
# interactive nodes) is still fully covered before the cap bites; each probe is one
# callFunctionOn, paid only under an overlay.
_MAX_OCCLUSION_PROBES = 400
_OVERLAY_MIN_Z = 10

_OVERLAY_PROBE_JS = (
    "(() => { try {"
    " const vw = window.innerWidth, vh = window.innerHeight;"
    " if (!vw || !vh) { return false; }"
    " let el = document.elementFromPoint(vw / 2, vh / 2);"
    " while (el && el !== document.documentElement && el !== document.body) {"
    "   const cs = getComputedStyle(el);"
    "   if (cs.position === 'fixed' || cs.position === 'sticky') {"
    "     const z = parseInt(cs.zIndex, 10);"
    "     const r = el.getBoundingClientRect();"
    f"    if (!isNaN(z) && z >= {_OVERLAY_MIN_Z}"
    "        && r.width >= vw * 0.8 && r.height >= vh * 0.6) { return true; }"
    "   }"
    "   el = el.parentElement;"
    " }"
    " return false;"
    " } catch (e) { return false; } })()"
)

# One generic occlusion hit-test, run ON the candidate element. Returns a small
# VERDICT string so the harness-side rule ("only an in-viewport centre whose hit
# belongs to a different subtree is covered") is explicit and testable:
#   'covered'   — a hit exists at the centre and neither contains the other
#   'clear'     — the element (or an ancestor/descendant) is what's at its centre
#   'offscreen' — the centre lies outside the viewport: NOT covered by definition
#                 (scrollIntoView reaches it; never clamp-and-probe)
#   'zero' / 'nohit' / 'nowin' / 'error' — degenerate probes, all fail-open
_COVERED_FN = (
    "function(){ try {"
    " const r = this.getBoundingClientRect();"
    " if (!r.width || !r.height) { return 'zero'; }"
    " const cx = r.left + r.width / 2, cy = r.top + r.height / 2;"
    " const doc = this.ownerDocument || document;"
    " const win = doc.defaultView;"
    " if (!win) { return 'nowin'; }"
    " if (cx < 0 || cy < 0 || cx >= win.innerWidth || cy >= win.innerHeight) {"
    "   return 'offscreen';"
    " }"
    " const root = (this.getRootNode && this.getRootNode().elementFromPoint)"
    "   ? this.getRootNode() : doc;"  # a shadow root hit-tests within itself
    " const hit = root.elementFromPoint(cx, cy);"
    " if (!hit) { return 'nohit'; }"
    " if (this.contains(hit) || hit.contains(this)) { return 'clear'; }"
    # The styled-label radio/checkbox pattern: a visually-hidden <input> (sr-only / opacity:0 /
    # .btn-check) whose clickable proxy is its OWN <label> — a SIBLING, not an ancestor, so the
    # centre hit-tests to that label (or a span inside it). That is the control's own affordance,
    # NOT occlusion by an overlay: a click on it drives the input. Bootstrap/Tailwind/MUI all
    # ship this shape, and it renders the modal's own slot/variant grid — pruning it would blind
    # the very flow the pass protects. Content-free: pure labelable-control DOM semantics.
    " try {"
    "   const lbls = this.labels ? Array.prototype.slice.call(this.labels) : [];"
    "   for (let i = 0; i < lbls.length; i++) {"
    "     const l = lbls[i];"
    "     if (l === hit || l.contains(hit) || hit.contains(l)) { return 'clear'; }"
    "   }"
    "   const hl = hit.closest ? hit.closest('label') : null;"
    "   if (hl && hl.control === this) { return 'clear'; }"
    " } catch (e2) {}"
    " return 'covered';"
    " } catch (e) { return 'error'; } }"
)


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
        await self._mark_covered(ax_nodes)
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
        object_id, node_id, session_id = await self._resolve(action.handle)
        if object_id is not None:
            if action.kind == _TYPE:
                await self._call_fn(object_id, _TYPE_FN, [{"value": action.text or ""}], session_id)
            elif action.kind == _SELECT:
                await self._call_fn(object_id, _SELECT_FN, [{"value": action.text}], session_id)
            elif action.kind == _SUBMIT:
                await self._call_fn(object_id, _SUBMIT_FN, session_id=session_id)
            else:  # click — the default verb
                await self._click(object_id, node_id, session_id)
        # A stale/unresolvable handle is an escalation, not a crash (§11.3): we
        # simply re-perceive; the caller sees the handle gone and re-captures.
        return await self.perceive()

    async def _click(self, object_id: str, node_id: int | None, session_id: str | None) -> None:
        """The click verb, TRUSTED input first: scroll the element into view, read its
        box model, and dispatch a real primary-button press+release at the centre —
        ``event.isTrusted`` is true, so widgets that ignore synthetic clicks (a React
        slot grid listening for genuine pointer input) respond. Falls back to the
        synthetic ``_CLICK_FN`` when the box model is unavailable/zero-area or the
        dispatch fails, so every element stays clickable on every transport."""
        await self._call_fn(object_id, _SCROLL_FN, session_id=session_id)
        if node_id is not None and await self._trusted_click(node_id, session_id):
            return
        await self._call_fn(object_id, _CLICK_FN, session_id=session_id)

    async def _trusted_click(self, node_id: int, session_id: str | None) -> bool:
        """One real (isTrusted) click at the element's box-model centre. True iff the
        press+release were dispatched; any failure — no/degenerate box model, a centre
        OUTSIDE the layout viewport (a stale box under async scroll), a raise from the
        transport — returns False so the caller falls back to the synthetic click
        (fail-open: the click always has an arm that fires, and the synthetic one hits the
        intended element regardless of where it sits)."""
        try:
            resp = await self._send(
                "DOM.getBoxModel", {"backendNodeId": node_id}, session_id=session_id
            )
            model = resp.get("model") if isinstance(resp, dict) else None
            quad = model.get("content") if isinstance(model, dict) else None
            if not isinstance(quad, list) or len(quad) < 8:
                return False
            xs = [float(quad[i]) for i in range(0, 8, 2)]
            ys = [float(quad[i]) for i in range(1, 8, 2)]
            if max(xs) - min(xs) <= 0 or max(ys) - min(ys) <= 0:
                return False  # zero-area — nothing a pointer could hit
            x, y = sum(xs) / 4.0, sum(ys) / 4.0
            if x < 0 or y < 0:
                return False  # centre off the viewport origin side — not dispatchable
            # A centre BELOW/RIGHT of the layout viewport means the box is stale (the scroll
            # hadn't landed when it was read) — dispatching there hits empty space and the
            # widget never fires. Fall back to the synthetic click, which targets the node
            # itself. `Page.getLayoutMetrics` is cheap; any failure skips the bound (fail-open).
            try:
                metrics = await self._send("Page.getLayoutMetrics", {}, session_id=session_id)
                vp = metrics.get("cssLayoutViewport") if isinstance(metrics, dict) else None
                vw = float(vp.get("clientWidth") or 0) if isinstance(vp, dict) else 0.0
                vh = float(vp.get("clientHeight") or 0) if isinstance(vp, dict) else 0.0
                if vw > 0 and vh > 0 and (x >= vw or y >= vh):
                    return False
            except Exception:  # noqa: BLE001 - can't read the viewport: skip the bound
                pass
            for event in ("mousePressed", "mouseReleased"):
                await self._send(
                    "Input.dispatchMouseEvent",
                    {"type": event, "x": x, "y": y, "button": "left", "clickCount": 1},
                    session_id=session_id,
                )
            return True
        except Exception:  # noqa: BLE001 - fail open: the synthetic click arm still fires
            return False

    # --- occlusion pass (see the block comment above _OVERLAY_PROBE_JS) -------

    async def _mark_covered(self, nodes: list[AxNode]) -> None:
        """Stamp ``covered=True`` on the interactive nodes an overlay occludes.
        Skipped entirely — zero probe traffic — when no candidates exist or the ONE
        ``Runtime.evaluate`` pre-probe says no full-viewport overlay is up. Only
        root-session nodes with a ``node_id`` are probed (an OOPIF's flat session
        routing is not portable across transports, so its nodes are left alone),
        capped at :data:`_MAX_OCCLUSION_PROBES`. Probe failures leave a node
        uncovered — fail-open, a broken probe never hides a control."""
        candidates: list[tuple[AxNode, int]] = []
        for n in nodes:
            nid = n.node_id
            if (
                isinstance(nid, int) and n.session_id is None
                and n.role in INTERACTIVE_ROLES and n.visible and not n.ignored
            ):
                candidates.append((n, nid))
        if not candidates:
            return
        if not await self._overlay_up():
            return  # the common path: no overlay, no probes
        for node, nid in candidates[:_MAX_OCCLUSION_PROBES]:
            node.covered = await self._probe_covered(nid)

    async def _overlay_up(self) -> bool:
        """The ONE cheap pre-probe: is a generic full-viewport overlay up? Any
        failure reads as 'no overlay' — the pass then costs nothing further."""
        try:
            resp = await self._send(
                "Runtime.evaluate",
                {"expression": _OVERLAY_PROBE_JS, "returnByValue": True},
            )
        except Exception:  # noqa: BLE001 - a broken pre-probe reads as 'no overlay'
            return False
        result = resp.get("result") if isinstance(resp, dict) else None
        return (result.get("value") is True) if isinstance(result, dict) else False

    async def _probe_covered(self, node_id: int) -> bool:
        """Hit-test ONE element (root session): covered iff the probe positively
        verdicts 'covered'. Every other verdict — 'offscreen' (centre outside the
        viewport: reachable via scrollIntoView, so NOT covered), degenerate probes,
        resolution failures, raises — keeps the node (fail-open)."""
        try:
            resp = await self._send("DOM.resolveNode", {"backendNodeId": node_id})
            obj = resp.get("object") if isinstance(resp, dict) else None
            object_id = obj.get("objectId") if isinstance(obj, dict) else None
            if not isinstance(object_id, str):
                return False
            out = await self._call_fn(object_id, _COVERED_FN)
            result = out.get("result") if isinstance(out, dict) else None
            verdict = result.get("value") if isinstance(result, dict) else None
            return verdict == "covered"
        except Exception:  # noqa: BLE001 - a broken probe must never hide a control
            return False

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

    async def _resolve(self, handle: str) -> tuple[str | None, int | None, str | None]:
        """Resolve an opaque handle to a live JS object id + its backend node id + its
        session, ACROSS boundaries: the handle map carries the element's GLOBAL backend
        DOM-node id (which ``DOM.resolveNode`` turns into an objectId regardless of
        shadow root / frame — and which the trusted click's ``DOM.getBoxModel`` needs)
        and, for an OOPIF control, the session it lives in (#126). ``(None, None,
        None)`` (unknown handle / no node id) is a re-capture signal."""
        locator = self._handle_map.get(handle)
        node_id = locator.get("node_id") if isinstance(locator, dict) else None
        session_id = locator.get("session_id") if isinstance(locator, dict) else None
        session_id = session_id if isinstance(session_id, str) else None
        if not isinstance(node_id, int):
            return None, None, None
        resp = await self._send("DOM.resolveNode", {"backendNodeId": node_id}, session_id=session_id)
        obj = resp.get("object") if isinstance(resp, dict) else None
        object_id = obj.get("objectId") if isinstance(obj, dict) else None
        return (object_id if isinstance(object_id, str) else None), node_id, session_id

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
