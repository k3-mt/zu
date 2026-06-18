"""SSRF guard for outbound fetches.

Zu reads untrusted web content, and the model chooses the URLs — so a hostile
page (or a hijacked model) can ask the runtime to fetch an internal address:
cloud metadata (169.254.169.254), localhost, or a service on the private
network. The real containment is the SandboxBackend (network egress policy);
until a fetch runs inside one, this denylist is the host-level backstop.

Default-deny for loopback / link-local / private / reserved ranges, on the
initial URL *and on every redirect hop* (the redirect is the classic bypass:
a public URL that 302s to 169.254.169.254). Opt out with
``ZU_HTTP_ALLOW_PRIVATE=1`` or ``HttpFetch(allow_private=True)`` for local dev
against localhost.

Known limitation: this resolves the host and checks the addresses, so there is
a DNS-rebinding TOCTOU window between check and connect. Closing it fully means
pinning the connection to the validated IP — a job for the sandbox's egress
layer, not this backstop.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = {"http", "https"}


class BlockedURLError(Exception):
    """Raised when a URL is refused by the SSRF guard."""


def _resolve_ips(host: str) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise BlockedURLError(f"could not resolve host {host!r}: {exc}") from exc
    return {str(info[4][0]) for info in infos}


def _ip_blocked_reason(ip_str: str) -> str | None:
    ip = ipaddress.ip_address(ip_str)
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local (incl. cloud metadata 169.254.169.254)"
    if ip.is_private:
        return "private"
    if ip.is_reserved:
        return "reserved"
    if ip.is_multicast:
        return "multicast"
    if ip.is_unspecified:
        return "unspecified"
    return None


def check_url(url: str, *, allow_private: bool | None = None) -> None:
    """Raise BlockedURLError if ``url`` should not be fetched.

    ``allow_private`` None consults the ``ZU_HTTP_ALLOW_PRIVATE`` env var;
    an explicit bool overrides it.
    """
    if allow_private is None:
        allow_private = os.environ.get("ZU_HTTP_ALLOW_PRIVATE") == "1"

    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise BlockedURLError(
            f"scheme {parts.scheme or '(none)'!r} not allowed; use http or https"
        )
    host = parts.hostname
    if not host:
        raise BlockedURLError(f"no host in URL {url!r}")

    if allow_private:
        return

    for ip in _resolve_ips(host):
        reason = _ip_blocked_reason(ip)
        if reason is not None:
            raise BlockedURLError(
                f"refusing to fetch {host!r} -> {ip} ({reason}); "
                "set ZU_HTTP_ALLOW_PRIVATE=1 to override for local development"
            )
