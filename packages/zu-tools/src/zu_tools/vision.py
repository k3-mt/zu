"""vision — the tier-4 capture tool: hand the policy PIXELS when a11y goes blind.

The Action Surface (tier 3, §11) is a fast, cheap default; its competence boundary
is a canvas/icon-heavy page the accessibility tree describes poorly. When it sets
``surface_blind``, the ``action-surface-blind`` detector ESCALATEs and the loop's
ladder climbs a tier. This tool is the rung that climb lands on: it captures the
SAME live page (the run-scoped browser session the Action Surface already opened)
as a screenshot and hands it to the policy as a :class:`zu_core.content.Image`, so
a VLM can act where the a11y surface could not.

``op=capture`` is deliberately THIN. It captures pixels and hands them over; it
does NOT detect elements in the image — that is a vision MODEL (§6/Phase 3), a
separate policy seam, not this tool's job. The decision rule (§4.5) again: a script
may *capture what is there* (the screenshot), but deciding *what is in it* is the
policy's judgment. So the capture step enumerates the pixels and stops.

``op=surface`` is the §4.4 VISION REDUCER rung: capture → an INJECTED vision
detector PROPOSES raw detections (the one irreducible model step) → the
DETERMINISTIC :func:`~zu_tools.vision_surface.reduce_vision_surface` DISPOSES,
reducing them to the SAME :class:`~zu_tools.action_surface.Surface`/core
``SurfaceView`` the a11y Action Surface produces — so patterns, the recognizer,
the policy, and pointer control work over it UNCHANGED (the modality-agnostic
point). The handle → click-point map is stored harness-side in the run registry,
so the pointer can act on a vision-detected element by HANDLE (never a pixel
coordinate). ``op=resolve`` returns a handle's click-point; a stale handle is an
escalation, not a crash. The detector is injected (a real vision model is the
caller's/config's job) — with NO detector wired, ``op=surface`` is honestly blind.
"""

from __future__ import annotations

import base64
from typing import Any

from zu_core.content import Image
from zu_core.ports import CAP_NET, CAP_SANDBOX, EGRESS_OPEN, BrowserSessionHandle

from ._session import attach, put_handle_map, resolve_handle, run_key
from .action_surface import Surface
from .vision_surface import (
    DEFAULT_CONFIDENCE_FLOOR,
    DEFAULT_MIN_AREA,
    VisionDetector,
    reduce_vision_surface,
)

_DEFAULT_IMAGE = "ghcr.io/k3-mt/zu-render-chromium:latest"


class VisionCapture:
    """Tier-4 tool: screenshot the live page so a VLM policy can act when blind.

    ``op=capture`` (default): reuse the run-scoped browser session (the SAME page
    the Action Surface was blind on), ask the container for a PNG, and return it as
    an :class:`Image` content part the policy reads via ``Observation.parts('image')``.
    A stale/closed session is an error, not a crash.
    """

    name = "vision"
    tier = 4  # the pixels tier; reached when the a11y surface signals blind (§11.4)
    schema = {
        "name": "vision",
        "description": (
            "SEE the live page as pixels when the accessibility surface goes 'blind' "
            "(a canvas, an unlabeled icon). op=capture returns a raw image you reason "
            "over directly. op=surface runs a vision detector over the screenshot and "
            "reduces it to the SAME flat list of affordances (handles a1,a2,… with "
            "labels) as action_surface — pick a handle and act on it with the pointer; "
            "op=resolve a handle to its click-point. If op=surface is 'blind' there is "
            "nothing actionable to see — escalate to a human (vision is the last tier)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["capture", "surface", "resolve"]},
                "full_page": {"type": "boolean",
                              "description": "capture the full scrollable page (default: the viewport)"},
                "handle": {"type": "string",
                           "description": "for op=resolve: the handle to resolve to a click-point"},
            },
        },
    }
    prompt_fragment = (
        "vision(op=surface): when action_surface is 'blind', screenshot the live page, "
        "detect its controls, and reduce to affordances (handles a1,a2,… with labels) you "
        "act on with the pointer. op=capture for raw pixels; resolve(handle) gives a "
        "click-point. 'blind' on op=surface means escalate to a human (last tier)."
    )
    capabilities = frozenset({CAP_NET, CAP_SANDBOX})
    egress = frozenset({EGRESS_OPEN})

    def __init__(
        self,
        session: BrowserSessionHandle | None = None,
        image: str = _DEFAULT_IMAGE,
        *,
        detector: VisionDetector | None = None,
        confidence_floor: float = DEFAULT_CONFIDENCE_FLOOR,
        min_area: float = DEFAULT_MIN_AREA,
        unlabeled_ratio: float = 0.5,
    ) -> None:
        # Like the pointer, vision acts on an ALREADY-OPEN page — it ATTACHES to the
        # run's SHARED session the Action Surface opened (so it shoots the page that
        # was blind), via the module-level run registry; never leasing a fresh,
        # page-less browser of its own. ``session`` is an explicit handle for tests.
        self._session = session
        self.image = image
        # The INJECTED perception seam: the one model step of op=surface. None ⇒ the
        # reducer has no detections to dispose of and op=surface is honestly blind
        # (a live vision model is the caller's/config's job, never CI's).
        self._detector = detector
        self._confidence_floor = confidence_floor
        self._min_area = min_area
        self._unlabeled_ratio = unlabeled_ratio
        # The offline reduce-only fallback for op=resolve (the run registry is the
        # authoritative cross-tool map when a run/session exists).
        self._handle_map: dict[str, dict] = {}

    def _acquire_session(self, ctx: Any) -> BrowserSessionHandle | None:
        if self._session is not None:
            return self._session
        return attach(run_key(ctx))

    async def __call__(
        self,
        ctx: Any,
        op: str = "capture",
        full_page: bool = False,
        handle: str | None = None,
    ) -> dict:
        if op == "resolve":
            return self._resolve_op(ctx, handle)
        if op not in ("capture", "surface"):
            return {"error": f"unknown op {op!r}; use capture/surface/resolve"}

        img, meta = await self._capture(ctx, full_page)
        if img is None:
            return meta  # an error dict, not a crash

        if op == "capture":
            # The Image content part rides on the observation; a VLM policy reads it
            # via Observation.parts('image'). The small vision metadata is the cheap
            # summary.
            return {"vision": meta, "image": img.model_dump(mode="json")}

        # op == "surface": the §4.4 reducer rung. The detector PROPOSES detections;
        # the deterministic reducer DISPOSES them into the SAME Surface as a11y.
        return self._surface(ctx, img, meta)

    async def _capture(self, ctx: Any, full_page: bool) -> tuple[Image | None, dict]:
        """Capture the live page as an Image (or return (None, error-dict))."""
        session = self._acquire_session(ctx)
        if session is None:
            return None, {"error": "vision needs an open browser session (open one with "
                                   "action_surface(op=open) or browser(op=open) first)"}
        resp = await session.send({"op": "screenshot", "full_page": full_page})
        if not isinstance(resp, dict) or resp.get("screenshot_b64") is None:
            err = resp.get("error") if isinstance(resp, dict) else "bad session response"
            return None, {"error": f"could not capture screenshot: {err}"}
        try:
            png = base64.b64decode(resp["screenshot_b64"])
        except (ValueError, TypeError) as exc:
            return None, {"error": f"screenshot was not valid base64: {exc}"}
        img = Image(data=png, mime=str(resp.get("mime", "image/png")))
        meta = {
            "url": resp.get("url"),
            "width": resp.get("width"),
            "height": resp.get("height"),
            "full_page": full_page,
        }
        return img, meta

    def _surface(self, ctx: Any, img: Image, meta: dict) -> dict:
        """Detector PROPOSES → deterministic reducer DISPOSES → emit a Surface."""
        if self._detector is None:
            # No model wired: there are no detections to dispose of, so the reduction
            # is honestly blind (last tier ⇒ escalate to a human). Never a crash, and
            # never a silently-empty surface.
            return {
                "vision_surface": Surface(
                    title="", url=str(meta.get("url") or ""),
                    blind=True,
                    blind_reason="no vision detector configured; cannot detect controls "
                                 "in the screenshot — escalate to a human",
                ).model_dump(exclude={"handle_map"}),
                "surface_blind": True,
                "image": img.model_dump(mode="json"),
            }
        detections = self._detector(img)
        w, h = meta.get("width"), meta.get("height")
        viewport = (float(w), float(h)) if isinstance(w, (int, float)) and isinstance(h, (int, float)) else None
        surface = reduce_vision_surface(
            detections,
            url=str(meta.get("url") or ""),
            viewport=viewport,
            confidence_floor=self._confidence_floor,
            min_area=self._min_area,
            unlabeled_ratio=self._unlabeled_ratio,
        )
        # The handle → click-point map is HARNESS-SIDE: stored in the run registry so
        # the pointer resolves the same handle the model emits, AND on the instance
        # for the offline reduce-only path. NEVER returned in the model-visible obs.
        self._handle_map = dict(surface.handle_map)
        put_handle_map(run_key(ctx), surface.handle_map)
        return {
            "vision_surface": surface.model_dump(exclude={"handle_map"}),
            "surface_blind": surface.blind,
            # Keep the pixels alongside so a VLM policy can still reason over them.
            "image": img.model_dump(mode="json"),
        }

    def _resolve_op(self, ctx: Any, handle: str | None) -> dict:
        """Resolve a vision handle to its durable click-point locator (harness-side).
        A stale/unknown handle is an ESCALATION signal, not a crash (§11.3)."""
        if not handle:
            return {"error": "op=resolve requires a handle"}
        locator = resolve_handle(run_key(ctx), handle) or self._handle_map.get(handle)
        if locator is None:
            return {"stale_handle": handle,
                    "error": f"handle {handle!r} is not on the current vision surface; re-capture"}
        return {"handle": handle, "locator": locator}
