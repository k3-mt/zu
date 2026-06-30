"""http_fetch — the tier-1 fetch tool. The cheapest action; try it first."""

from __future__ import annotations

import os
from urllib.parse import urljoin

import httpx

from zu_core.ports import CAP_NET, EGRESS_OPEN
from zu_core.security import SANDBOX_ENV

from .net import BlockedURLError, PinnedTransport, check_url

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
    # A general web fetcher reaches model-chosen URLs, so it declares open egress
    # (EGRESS_OPEN) — the high-trust case PHILOSOPHY.md §6 says earns review. Its
    # host-level SSRF guard (net.check_url) is what bounds the open egress until a
    # sandbox scopes it; it declares no fs/subprocess capability.
    capabilities = frozenset({CAP_NET})
    egress = frozenset({EGRESS_OPEN})

    def __init__(
        self,
        allow_private: bool | None = None,
        max_redirects: int = 5,
        max_bytes: int = _DEFAULT_MAX_BYTES,
        transport: httpx.AsyncBaseTransport | None = None,
        allowed_domains: list[str] | None = None,
    ) -> None:
        # allow_private None -> consult ZU_HTTP_ALLOW_PRIVATE (see net.check_url).
        self.allow_private = allow_private
        self.max_redirects = max_redirects
        self.max_bytes = max_bytes
        # transport is a testability seam (httpx.MockTransport); None -> real net.
        self._transport = transport
        # The per-agent positive navigation allowlist (issue #74): None ⇒ unset
        # (the SSRF backstop alone governs). Enforced on the initial URL AND every
        # redirect hop via check_url, in addition to the pre-exec gate.
        self.allowed_domains = allowed_domains

    def _contained(self) -> bool:
        """Inside the Zu sandbox the egress proxy on the internal (default-DROP)
        network IS the boundary, so route through it (honor ``HTTP(S)_PROXY``) and
        skip the host-side SSRF/DNS-pin guard — which is both redundant there and,
        by resolving + connecting to the IP directly, would BYPASS the proxy and hit
        the default-DROP, making even an allowlisted host unreachable. An injected
        transport (tests) always takes the guarded path."""
        return bool(os.environ.get(SANDBOX_ENV)) and self._transport is None

    async def __call__(self, ctx, url: str) -> dict:
        if self._contained():
            return await self._fetch_via_proxy(url)
        return await self._fetch_guarded(url)

    async def _fetch_via_proxy(self, url: str) -> dict:
        """Contained path: a plain proxy-respecting client. The proxy enforces the
        egress allowlist (a refused host fails the connection / returns non-2xx)
        and re-checks each redirect hop by host, so no host-side guard is needed."""
        # The positive navigation allowlist (issue #74) is a tool-level guarantee,
        # independent of the proxy's SSRF egress scoping: enforce the initial host
        # here too so a contained run honours allowed_domains identically.
        if self.allowed_domains is not None:
            from urllib.parse import urlsplit

            from .net import _check_allowlist

            _check_allowlist(urlsplit(url).hostname or "", self.allowed_domains)
        try:
            async with httpx.AsyncClient(
                follow_redirects=True, timeout=20, trust_env=True,
                max_redirects=self.max_redirects,
            ) as c:
                async with c.stream("GET", url) as r:
                    html = await self._read_capped(r, url)
                    return {"status": r.status_code, "html": html, "url": str(r.url)}
        except httpx.HTTPError as exc:
            # A proxy refusal (off-allowlist host) surfaces as a connect/proxy error;
            # report it as a blocked fetch — the proxy already logged it out of band.
            raise BlockedURLError(
                f"egress to {url!r} refused or unreachable via the sandbox proxy: {exc}"
            ) from exc

    async def _fetch_guarded(self, url: str) -> dict:
        # Uncontained (host) path. Validate the initial URL and every redirect hop:
        # a public URL that 302s to an internal address is the classic SSRF bypass,
        # so we follow redirects manually and re-check each Location before it.
        check_url(url, allow_private=self.allow_private, allowed_domains=self.allowed_domains)
        current = url
        # Default to the DNS-pinning transport: check_url is an early reject, but
        # the transport is what *closes* the rebind TOCTOU by connecting only to a
        # validated IP. An injected transport (tests' MockTransport) is used as-is.
        transport = self._transport or PinnedTransport(allow_private=self.allow_private)
        async with httpx.AsyncClient(
            follow_redirects=False, timeout=20, transport=transport
        ) as c:
            for _ in range(self.max_redirects + 1):
                # Stream so we can stop reading once the body exceeds max_bytes
                # instead of buffering an unbounded (or decompression-bombed) page.
                async with c.stream("GET", current) as r:
                    if r.is_redirect and r.headers.get("location"):
                        # Resolve the next hop against the original *hostname* URL
                        # (``current``), NOT ``r.url`` — PinnedTransport rewrites
                        # the request host to the pinned IP, so a relative Location
                        # joined to ``r.url`` would carry the IP as host and the
                        # next TLS handshake would verify the cert against the IP.
                        nxt = urljoin(current, r.headers["location"])
                        check_url(
                            nxt, allow_private=self.allow_private,
                            allowed_domains=self.allowed_domains,
                        )
                        current = nxt
                        continue
                    html = await self._read_capped(r, current)
                    # Report the hostname URL we fetched, not r.url (the IP form).
                    return {"status": r.status_code, "html": html, "url": current}
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
