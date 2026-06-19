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

from zu_core.ports import SandboxBackend, ToolCall

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
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    }
    prompt_fragment = (
        "render_dom(url): render a page in a real browser (JS executed). "
        "More expensive than http_fetch; use when a page needs JavaScript."
    )

    def __init__(self, backend: SandboxBackend | None = None, image: str = _DEFAULT_IMAGE) -> None:
        # backend None -> the local-docker default, imported lazily so this
        # module (and tier-1-only deployments) need not pull in the backend
        # package or a Docker client until a render is actually attempted.
        self._backend = backend
        self.image = image

    def _resolve_backend(self) -> SandboxBackend:
        if self._backend is None:
            from zu_backends.local_docker import LocalDockerBackend

            self._backend = LocalDockerBackend()
        return self._backend

    async def __call__(self, ctx: Any, url: str) -> dict:
        backend = self._resolve_backend()
        # Lease a sandbox for the render and always tear it down — a browser
        # container is expensive and must not leak even if the render raises.
        # ``network`` is required: a browser with egress disabled cannot fetch
        # the page it is asked to render. The sandbox is *where* egress is
        # controlled (vs. tier 1's host-level SSRF guard); scoping that egress
        # to the target — an allowlist / DNS-pinned connection — is the deferred
        # SandboxBackend egress-policy work.
        sandbox = await backend.launch({"image": self.image, "tier": self.tier, "network": True})
        try:
            obs = await backend.exec(sandbox, ToolCall(name=self.name, args={"url": url}))
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
