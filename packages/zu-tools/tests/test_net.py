"""The SSRF guard blocks internal targets on the initial URL and redirect hops."""

from __future__ import annotations

import pytest

from zu_tools.net import BlockedURLError, check_url


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
