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


def test_contained_mode_selected_only_inside_the_sandbox(monkeypatch) -> None:
    # Inside the box (ZU_SANDBOXED set, no injected transport) the proxy-routed
    # path is used; an injected transport (tests) or a bare host always takes the
    # guarded path so the host-side SSRF/DNS-pin guard still applies off-sandbox.
    monkeypatch.delenv("ZU_SANDBOXED", raising=False)
    assert HttpFetch()._contained() is False                      # bare host
    monkeypatch.setenv("ZU_SANDBOXED", "1")
    assert HttpFetch()._contained() is True                       # in the box
    assert HttpFetch(transport=_transport(b"x"))._contained() is False  # injected -> guarded


async def test_relative_redirect_resolves_against_hostname_not_pinned_ip(monkeypatch) -> None:
    # Regression: PinnedTransport rewrites the request host to the pinned IP. A
    # relative redirect Location must be resolved against the original HOSTNAME,
    # not r.url (the IP form) — else the next hop carries the IP as host and TLS
    # verifies the cert against the IP. The reported url must be the hostname too.
    import zu_tools.net as net_mod
    from zu_tools.net import PinnedTransport

    monkeypatch.setattr(net_mod, "_resolve_ips", lambda host: {"9.9.9.9"})
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.url.path, request.extensions.get("sni_hostname")))
        if request.url.path == "/Book":
            return httpx.Response(301, headers={"location": "/book-lower"})
        return httpx.Response(200, text="<html>ok</html>")

    fetch = HttpFetch(transport=PinnedTransport(allow_private=False, inner=httpx.MockTransport(handler)))
    out = await fetch(None, "https://shop.example/Book")
    assert out["status"] == 200
    assert out["url"] == "https://shop.example/book-lower"        # hostname kept through redirect
    assert [host for host, _, _ in seen] == ["9.9.9.9", "9.9.9.9"]  # both hops pinned to the IP
    assert all(sni == "shop.example" for _, _, sni in seen)        # SNI stayed the hostname
    assert seen[1][1] == "/book-lower"                             # redirect resolved correctly


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
