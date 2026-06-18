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
