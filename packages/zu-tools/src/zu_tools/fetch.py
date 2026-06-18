"""http_fetch — the tier-1 fetch tool. The cheapest action; try it first."""

from __future__ import annotations

from urllib.parse import urljoin

import httpx

from .net import check_url


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

    def __init__(self, allow_private: bool | None = None, max_redirects: int = 5) -> None:
        # allow_private None -> consult ZU_HTTP_ALLOW_PRIVATE (see net.check_url).
        self.allow_private = allow_private
        self.max_redirects = max_redirects

    async def __call__(self, ctx, url: str) -> dict:
        # Validate the initial URL and every redirect hop: a public URL that
        # 302s to an internal address is the classic SSRF bypass, so we follow
        # redirects manually and re-check each Location before requesting it.
        check_url(url, allow_private=self.allow_private)
        current = url
        async with httpx.AsyncClient(follow_redirects=False, timeout=20) as c:
            for _ in range(self.max_redirects + 1):
                r = await c.get(current)
                location = r.headers.get("location") if r.is_redirect else None
                if not location:
                    return {"status": r.status_code, "html": r.text, "url": str(r.url)}
                nxt = urljoin(str(r.url), location)
                check_url(nxt, allow_private=self.allow_private)
                current = nxt
        from .net import BlockedURLError

        raise BlockedURLError(f"too many redirects (> {self.max_redirects}) starting from {url!r}")
