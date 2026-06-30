"""#74 — check_url enforces a POSITIVE per-agent navigation allowlist on the initial
URL AND every redirect hop, on top of the SSRF default-deny backstop.
"""

from __future__ import annotations

import httpx
import pytest

from zu_tools.fetch import HttpFetch
from zu_tools.net import BlockedURLError, check_url


def test_check_url_allows_on_allowlist_host() -> None:
    # allow_private skips DNS; the allowlist is still enforced (a positive guarantee).
    check_url("https://api.good.example/v1", allow_private=True,
              allowed_domains=["*.good.example"])


def test_check_url_denies_off_allowlist_host() -> None:
    with pytest.raises(BlockedURLError) as exc:
        check_url("https://evil.test/x", allow_private=True,
                  allowed_domains=["*.good.example"])
    assert exc.value.kind == "off_allowlist"


def test_check_url_no_allowlist_is_unchanged() -> None:
    # No allowlist configured -> only the SSRF backstop governs (any public host OK).
    check_url("https://anything.test/x", allow_private=True, allowed_domains=None)


async def test_redirect_to_off_allowlist_host_blocked_at_the_hop() -> None:
    # The initial host is on-allowlist; the 302 target is NOT. The per-hop re-check
    # must refuse the Location before requesting it (not just the initial URL).
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "evil.test":
            return httpx.Response(200, content=b"<html>pwned</html>")
        return httpx.Response(302, headers={"location": "https://evil.test/x"})

    fetch = HttpFetch(allow_private=True, allowed_domains=["*.good.example"],
                      transport=httpx.MockTransport(handler))
    with pytest.raises(BlockedURLError) as exc:
        await fetch(None, "https://api.good.example/start")
    assert exc.value.kind == "off_allowlist"


async def test_on_allowlist_redirect_proceeds() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/final":
            return httpx.Response(200, content=b"<html>ok</html>")
        return httpx.Response(302, headers={"location": "https://b.good.example/final"})

    fetch = HttpFetch(allow_private=True, allowed_domains=["*.good.example"],
                      transport=httpx.MockTransport(handler))
    out = await fetch(None, "https://a.good.example/start")
    assert out["status"] == 200
