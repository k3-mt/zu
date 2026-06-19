"""The SSRF guard blocks internal targets on the initial URL and redirect hops."""

from __future__ import annotations

import httpx
import pytest

from zu_tools import net as net_mod
from zu_tools.net import BlockedURLError, PinnedTransport, check_url, pin_ip


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/admin",
        "http://169.254.169.254/latest/meta-data/",  # cloud metadata (link-local)
        "http://10.0.0.5/",
        "http://192.168.1.1/",
        "https://[::1]/",  # IPv6 loopback
        "http://[::ffff:169.254.169.254]/",  # IPv4-mapped metadata (mapped-IPv6 bypass)
        "http://[::ffff:127.0.0.1]/",  # IPv4-mapped loopback
        "http://[::ffff:10.0.0.1]/",  # IPv4-mapped private
        "http://[2002:a9fe:a9fe::]/",  # 6to4-wrapped 169.254.x.x
        "http://[64:ff9b::a9fe:a9fe]/",  # NAT64-wrapped metadata (non-global backstop)
    ],
)
def test_blocks_internal_targets(url: str) -> None:
    with pytest.raises(BlockedURLError):
        check_url(url, allow_private=False)


@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "gopher://x/"])
def test_blocks_non_http_schemes(url: str) -> None:
    with pytest.raises(BlockedURLError):
        check_url(url, allow_private=False)


def test_allows_public_ip() -> None:
    # 8.8.8.8 is a literal public address; getaddrinfo returns it without network.
    check_url("http://8.8.8.8/", allow_private=False)  # no raise


def test_opt_out_allows_private() -> None:
    check_url("http://127.0.0.1/", allow_private=True)  # no raise


def test_env_opt_out(monkeypatch) -> None:
    monkeypatch.setenv("ZU_HTTP_ALLOW_PRIVATE", "1")
    check_url("http://127.0.0.1/", allow_private=None)  # consults env -> allowed


# --- DNS-rebinding pin (PinnedTransport) -------------------------------------


class _CaptureTransport(httpx.AsyncBaseTransport):
    """Inner transport that records the request the pin produced and returns 200."""

    def __init__(self) -> None:
        self.request: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return httpx.Response(200, text="ok")


async def test_pin_connects_to_validated_ip_keeps_host_and_sni(monkeypatch) -> None:
    # The connection is pinned to the resolved IP, but the original hostname is
    # kept for the Host header and TLS SNI (so certs still validate).
    monkeypatch.setattr(net_mod, "_resolve_ips", lambda host: {"93.184.216.34"})
    cap = _CaptureTransport()
    t = PinnedTransport(allow_private=False, inner=cap)
    async with httpx.AsyncClient(transport=t) as c:
        r = await c.get("https://example.com/page")
    assert r.status_code == 200
    assert cap.request is not None
    assert cap.request.url.host == "93.184.216.34"          # connected to the validated IP
    assert cap.request.headers["Host"] == "example.com"      # original host preserved
    assert cap.request.extensions.get("sni_hostname") == "example.com"  # TLS SNI preserved


async def test_pin_refuses_a_rebind_to_an_internal_ip(monkeypatch) -> None:
    # The classic rebind: check_url saw a public IP, but at connect time the host
    # now resolves to metadata. The transport does the authoritative lookup and
    # refuses — closing the TOCTOU.
    monkeypatch.setattr(net_mod, "_resolve_ips", lambda host: {"169.254.169.254"})
    t = PinnedTransport(allow_private=False, inner=_CaptureTransport())
    async with httpx.AsyncClient(transport=t) as c:
        with pytest.raises(BlockedURLError):
            await c.get("https://rebind.example/")


async def test_pin_skipped_when_allow_private(monkeypatch) -> None:
    # Local dev: allow_private skips pinning so localhost still works.
    cap = _CaptureTransport()
    t = PinnedTransport(allow_private=True, inner=cap)
    async with httpx.AsyncClient(transport=t) as c:
        await c.get("http://localhost:8080/")
    assert cap.request is not None and cap.request.url.host == "localhost"  # unchanged


def test_pin_ip_returns_validated_and_prefers_ipv4(monkeypatch) -> None:
    monkeypatch.setattr(net_mod, "_resolve_ips", lambda host: {"2606:2800:220::1", "93.184.216.34"})
    assert pin_ip("example.com") == "93.184.216.34"  # IPv4 preferred for the pin


def test_pin_ip_raises_on_internal(monkeypatch) -> None:
    monkeypatch.setattr(net_mod, "_resolve_ips", lambda host: {"10.0.0.5"})
    with pytest.raises(BlockedURLError):
        pin_ip("internal.example")
