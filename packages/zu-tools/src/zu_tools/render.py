"""render_dom — the tier-2 browser tool.

The escalation target when a JavaScript page defeats the tier-1 ``http_fetch``:
it renders the URL in a real headless browser (JS executed) and returns the
resulting DOM. The browser does not run in this process — it runs inside a
sandbox obtained through the :class:`SandboxBackend` port, so the live,
unpredictable part (a real browser) is isolated behind a seam the tests can
freeze, exactly as ``http_fetch`` isolates the network behind an httpx
transport.

By default the sandbox is the local-docker backend, so an installed Zu renders
out of the box. A test (or build step 8 config) injects a different backend —
including a scripted one that returns a saved rendered page, which is how the
escalation ladder is proven offline.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from zu_core.ports import CAP_NET, CAP_SANDBOX, EGRESS_OPEN, SandboxBackend, ToolCall

from .net import validate_and_pin

# Default browser viewport. Chromium otherwise falls back to Playwright's
# implicit 1280x720; we set it explicitly so the rendered DOM is reproducible
# and a caller can override it (responsive pages branch on width).
_DEFAULT_WIDTH = 1280
_DEFAULT_HEIGHT = 720

# The sandbox image is a spec detail, not a hard-coded constant in the loop:
# the backend launches it. The published Playwright/Chromium image is the default
# tier-2 environment (built from images/render-chromium in this repo); swap it via
# RenderDom(image=...) or config without touching the loop.
_DEFAULT_IMAGE = "ghcr.io/k3-mt/zu-render-chromium:latest"


class RenderDom:
    name = "render_dom"
    tier = 2  # unlocked only after a detector escalates off tier 1
    schema = {
        "name": "render_dom",
        "description": "Render a URL in a headless browser and return the DOM after JS executes.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string"},
                "width": {"type": "integer", "description": "viewport width in px (optional)"},
                "height": {"type": "integer", "description": "viewport height in px (optional)"},
            },
            "required": ["url"],
        },
    }
    prompt_fragment = (
        "render_dom(url): render a page in a real browser (JS executed). "
        "More expensive than http_fetch; use when a page needs JavaScript."
    )
    # Provisions a sandbox (CAP_SANDBOX) and renders a model-chosen URL inside
    # it, so it declares open egress (the browser must reach the page). The
    # sandbox is where that egress is *enforced* and where scoping it to the
    # target is the deferred egress-policy work — but the declaration lives here.
    capabilities = frozenset({CAP_NET, CAP_SANDBOX})
    egress = frozenset({EGRESS_OPEN})

    def __init__(
        self,
        backend: SandboxBackend | None = None,
        image: str = _DEFAULT_IMAGE,
        *,
        allow_private: bool | None = None,
    ) -> None:
        # backend None -> the local-docker default, imported lazily so this
        # module (and tier-1-only deployments) need not pull in the backend
        # package or a Docker client until a render is actually attempted.
        self._backend = backend
        self.image = image
        # Mirrors HttpFetch: None consults ZU_HTTP_ALLOW_PRIVATE; True skips the
        # SSRF DNS check for local dev (and lets tests use a non-resolvable host).
        self.allow_private = allow_private

    def _resolve_backend(self) -> SandboxBackend:
        if self._backend is None:
            from zu_backends.local_docker import LocalDockerBackend

            self._backend = LocalDockerBackend()
        return self._backend

    async def __call__(
        self, ctx: Any, url: str, width: int | None = None, height: int | None = None
    ) -> dict:
        # Apply the same host-level SSRF backstop tier-1 http_fetch uses, *before*
        # leasing a browser: escalating to tier 2 must not become a way to fetch
        # an internal address (cloud metadata, loopback, RFC1918) with the guard
        # bypassed. ``validate_and_pin`` does the scheme/host check, the SSRF
        # validation, AND returns the pinned IP from a SINGLE host resolution —
        # so the target's DNS pin below uses the exact address that was validated,
        # closing the double-resolve TOCTOU that a separate check_url + pin_ip
        # opened. Raises BlockedURLError (a SecurityBlock) → the loop records a
        # harness.defense.blocked event and surfaces it as an error observation.
        #
        # Scope: this is the per-target rebind backstop, identical in strength to
        # tier 1. It does NOT scope the *sandbox's* egress — a page's in-browser
        # redirects and subresources can still reach other hosts unvalidated.
        # Full egress allowlisting (blocking those) needs a firewall-capable
        # SandboxBackend and remains deferred; the loud declaration is EGRESS_OPEN.
        pinned_ip = validate_and_pin(url, allow_private=self.allow_private)
        backend = self._resolve_backend()
        # Lease a sandbox for the render and always tear it down — a browser
        # container is expensive and must not leak even if the render raises.
        # ``network`` is required: a browser with egress disabled cannot fetch
        # the page it is asked to render.
        spec: dict[str, Any] = {"image": self.image, "tier": self.tier, "network": True}
        # Pin the container's DNS for the target host to the validated IP, so the
        # browser cannot be DNS-rebound to an internal address at connect time —
        # the tier-2 analogue of tier-1's PinnedTransport. ``pinned_ip`` is None
        # only when allow_private skips pinning (local dev).
        host = urlsplit(url).hostname
        if pinned_ip is not None and host:
            spec["extra_hosts"] = {host: pinned_ip}
        sandbox = await backend.launch(spec)
        args: dict[str, Any] = {
            "url": url,
            "width": int(width) if width else _DEFAULT_WIDTH,
            "height": int(height) if height else _DEFAULT_HEIGHT,
        }
        try:
            obs = await backend.exec(sandbox, ToolCall(name=self.name, args=args))
        finally:
            await backend.destroy(sandbox)
        # Normalise to the same observation shape http_fetch produces, so the
        # loop's content handling and the detectors are tool-agnostic.
        return {
            "status": obs.get("status", 200),
            "html": obs.get("html", ""),
            "url": obs.get("url", url),
            "rendered": True,
        }
