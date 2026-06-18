"""http_fetch — the SSRF guard is covered in test_net; here we lock the
response-size cap that stops an untrusted (or decompression-bombed) page from
being read into memory unbounded.

A MockTransport stands in for the network, and allow_private=True skips DNS so
the test stays fully offline.
"""

from __future__ import annotations

import httpx
import pytest

from zu_tools.fetch import HttpFetch
from zu_tools.net import BlockedURLError


def _transport(content: bytes, status: int = 200) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=content)

    return httpx.MockTransport(handler)


async def test_body_under_cap_is_returned() -> None:
    fetch = HttpFetch(allow_private=True, max_bytes=1000, transport=_transport(b"<html>ok</html>"))
    out = await fetch(None, "http://localhost/page")
    assert out["status"] == 200
    assert out["html"] == "<html>ok</html>"


async def test_body_over_cap_is_refused() -> None:
    fetch = HttpFetch(allow_private=True, max_bytes=100, transport=_transport(b"x" * 5000))
    with pytest.raises(BlockedURLError):
        await fetch(None, "http://localhost/huge")


async def test_initial_url_still_ssrf_checked() -> None:
    # The cap doesn't weaken the SSRF guard: an internal target is refused
    # before any request is made (no transport needed).
    fetch = HttpFetch(allow_private=False, transport=_transport(b"unused"))
    with pytest.raises(BlockedURLError):
        await fetch(None, "http://127.0.0.1/")


async def test_redirect_to_internal_is_blocked() -> None:
    # The classic SSRF bypass: a public URL that 302s to an internal address.
    # The per-hop re-check must refuse the Location before requesting it. This
    # exercises the redirect loop end-to-end, not just check_url in isolation.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data/"})

    fetch = HttpFetch(allow_private=False, transport=httpx.MockTransport(handler))
    with pytest.raises(BlockedURLError):
        await fetch(None, "http://8.8.8.8/")  # public start, internal redirect target
