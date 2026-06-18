"""http_fetch — the tier-1 fetch tool. The cheapest action; try it first."""

from __future__ import annotations

from urllib.parse import urljoin

import httpx

from .net import BlockedURLError, check_url

# Default cap on a single fetched body (decompressed). Untrusted pages can be
# arbitrarily large, and httpx transparently decompresses, so a small gzip can
# expand to gigabytes — cap the bytes we read, not just the bytes on the wire.
_DEFAULT_MAX_BYTES = 5_000_000


class HttpFetch:
    name = "http_fetch"
    tier = 1  # the cheapest action; offered from the start of every run
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

    def __init__(
        self,
        allow_private: bool | None = None,
        max_redirects: int = 5,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        # allow_private None -> consult ZU_HTTP_ALLOW_PRIVATE (see net.check_url).
        self.allow_private = allow_private
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        # transport is a testability seam (httpx.MockTransport); None -> real net.
        self._transport = transport

    async def __call__(self, ctx, url: str) -> dict:
        # Validate the initial URL and every redirect hop: a public URL that
        # 302s to an internal address is the classic SSRF bypass, so we follow
        # redirects manually and re-check each Location before requesting it.
        check_url(url, allow_private=self.allow_private)
        current = url
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=20, transport=self._transport
        ) as c:
            for _ in range(self.max_redirects + 1):
                # Stream so we can stop reading once the body exceeds max_bytes
                # instead of buffering an unbounded (or decompression-bombed) page.
                async with c.stream("GET", current) as r:
                    if r.is_redirect and r.headers.get("location"):
                        nxt = urljoin(str(r.url), r.headers["location"])
                        check_url(nxt, allow_private=self.allow_private)
                        current = nxt
                        continue
                    html = await self._read_capped(r, current)
                    return {"status": r.status_code, "html": html, "url": str(r.url)}
        raise BlockedURLError(f"too many redirects (> {self.max_redirects}) starting from {url!r}")

    async def _read_capped(self, r: httpx.Response, url: str) -> str:
        """Read the (decompressed) body up to max_bytes; refuse if it overflows."""
        chunks: list[bytes] = []
        total = 0
        async for chunk in r.aiter_bytes():
            total += len(chunk)
            if total > self.max_bytes:
                raise BlockedURLError(
                    f"response from {url!r} exceeds max_bytes ({self.max_bytes}); "
                    "raise HttpFetch(max_bytes=...) if a larger page is expected"
                )
            chunks.append(chunk)
        return b"".join(chunks).decode(r.encoding or "utf-8", errors="replace")
