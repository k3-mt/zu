"""vision — the tier-4 capture tool: hand the policy PIXELS when a11y goes blind.

The Action Surface (tier 3, §11) is a fast, cheap default; its competence boundary
is a canvas/icon-heavy page the accessibility tree describes poorly. When it sets
``surface_blind``, the ``action-surface-blind`` detector ESCALATEs and the loop's
ladder climbs a tier. This tool is the rung that climb lands on: it captures the
SAME live page (the run-scoped browser session the Action Surface already opened)
as a screenshot and hands it to the policy as a :class:`zu_core.content.Image`, so
a VLM can act where the a11y surface could not.

It is deliberately THIN. It captures pixels and hands them over; it does NOT detect
elements in the image — that is a vision MODEL (§6/Phase 3), a separate policy
seam, not this tool's job. The decision rule (§4.5) again: a script may *capture
what is there* (the screenshot), but deciding *what is in it* is the policy's
judgment. So this enumerates the pixels and stops.
"""

from __future__ import annotations

import base64
from typing import Any

from zu_core.content import Image
from zu_core.ports import CAP_NET, CAP_SANDBOX, EGRESS_OPEN, BrowserSessionHandle

from ._session import attach, run_key

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
            "Capture a SCREENSHOT of the live page as pixels so you can SEE what the "
            "accessibility surface could not describe (a canvas, an unlabeled icon). "
            "op=capture returns an image you can reason over directly. Use it when "
            "action_surface comes back 'blind'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["capture"]},
                "full_page": {"type": "boolean",
                              "description": "capture the full scrollable page (default: the viewport)"},
            },
        },
    }
    prompt_fragment = (
        "vision(op=capture): screenshot the live page as pixels when the a11y surface is "
        "'blind' (a canvas/icon page). You get an image to reason over — capture only; you "
        "decide what is in it."
    )
    capabilities = frozenset({CAP_NET, CAP_SANDBOX})
    egress = frozenset({EGRESS_OPEN})

    def __init__(
        self,
        session: BrowserSessionHandle | None = None,
        image: str = _DEFAULT_IMAGE,
    ) -> None:
        # Like the pointer, vision acts on an ALREADY-OPEN page — it ATTACHES to the
        # run's SHARED session the Action Surface opened (so it shoots the page that
        # was blind), via the module-level run registry; never leasing a fresh,
        # page-less browser of its own. ``session`` is an explicit handle for tests.
        self._session = session
        self.image = image

    def _acquire_session(self, ctx: Any) -> BrowserSessionHandle | None:
        if self._session is not None:
            return self._session
        return attach(run_key(ctx))

    async def __call__(self, ctx: Any, op: str = "capture", full_page: bool = False) -> dict:
        if op != "capture":
            return {"error": f"unknown op {op!r}; use capture"}
        session = self._acquire_session(ctx)
        if session is None:
            return {"error": "vision needs an open browser session (open one with "
                              "action_surface(op=open) or browser(op=open) first)"}
        resp = await session.send({"op": "screenshot", "full_page": full_page})
        if not isinstance(resp, dict) or resp.get("screenshot_b64") is None:
            err = resp.get("error") if isinstance(resp, dict) else "bad session response"
            return {"error": f"could not capture screenshot: {err}"}
        try:
            png = base64.b64decode(resp["screenshot_b64"])
        except (ValueError, TypeError) as exc:
            return {"error": f"screenshot was not valid base64: {exc}"}
        img = Image(data=png, mime=str(resp.get("mime", "image/png")))
        # The Image content part rides on the observation; a VLM policy reads it via
        # Observation.parts('image'). The small vision metadata is the cheap summary.
        return {
            "vision": {
                "url": resp.get("url"),
                "width": resp.get("width"),
                "height": resp.get("height"),
                "full_page": full_page,
            },
            "image": img.model_dump(mode="json"),
        }
