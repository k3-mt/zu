"""http_fetch — the tier-1 fetch tool. The cheapest action; try it first."""

from __future__ import annotations

import httpx


class HttpFetch:
    name = "http_fetch"
    schema = {
        "name": "http_fetch",
        "description": "Fetch a URL and return its raw HTML.",
        "parameters": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    }
    prompt_fragment = "http_fetch(url): fetch a page's raw HTML. Cheapest; try first."

    async def __call__(self, ctx, url: str) -> dict:
        async with httpx.AsyncClient(follow_redirects=True, timeout=20) as c:
            r = await c.get(url)
        return {"status": r.status_code, "html": r.text, "url": str(r.url)}
