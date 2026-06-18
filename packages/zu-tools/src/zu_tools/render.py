"""render_dom — the tier-2 browser tool (build step 5).

Runs a headless browser inside a sandbox (via the SandboxBackend port) and
returns the rendered DOM — the escalation target when a JavaScript page
defeats the tier-1 http_fetch. Importable now for discovery; the live render
is wired in build step 5 behind the local-docker backend.
"""

from __future__ import annotations


class RenderDom:
    name = "render_dom"
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

    async def __call__(self, ctx, url: str) -> dict:
        raise NotImplementedError(
            "render_dom is build step 5: drive a headless browser via the "
            "SandboxBackend port (local-docker adapter) and return the rendered DOM."
        )
