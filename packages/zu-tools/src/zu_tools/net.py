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

import httpx

from zu_core.security import SecurityBlock

_ALLOWED_SCHEMES = {"http", "https"}


class BlockedURLError(SecurityBlock):
    """Raised when a URL is refused by the egress guard. A ``SecurityBlock``, so
    the loop records it as a ``harness.defense.blocked`` event — a refused fetch
    is a contained attempt, not a silent failure. ``kind`` defaults to
    ``"fetch_blocked"``; the SSRF path sets ``kind="ssrf"`` and the target host."""

    kind = "fetch_blocked"


def _check_allowlist(host: str, allowed_domains: list[str] | None) -> None:
    """Enforce a POSITIVE per-agent navigation allowlist (issue #74) ON TOP of the
    SSRF default-deny backstop. ``allowed_domains`` is a list of wildcard host
    patterns (``*.example.com``); ``None`` ⇒ no allowlist configured (the backstop
    alone governs). When configured, a host that matches NO pattern is refused —
    enforced here so it holds on the initial URL AND on every redirect hop, not just
    the first navigation. Uses the SAME shared matcher the pre-exec gate uses, so
    the gate and this hop-level check cannot drift."""
    if allowed_domains is None:
        return
    from zu_core.hosts import host_matches_any

    if not host_matches_any(host, allowed_domains):
        raise BlockedURLError(
            f"refusing to navigate to {host!r}: not in the agent's allowed_domains "
            f"allowlist {list(allowed_domains)!r}",
            kind="off_allowlist",
            target=host,
        )


def _resolve_ips(host: str) -> set[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise BlockedURLError(f"could not resolve host {host!r}: {exc}") from exc
    return {str(info[4][0]) for info in infos}


def _ip_blocked_reason(ip_str: str) -> str | None:
    ip = ipaddress.ip_address(ip_str)
    # Unwrap IPv6 forms that embed an IPv4 address (``::ffff:169.254.169.254``,
    # 6to4 ``2002::/16``) and re-check the inner address, so a mapped/tunnelled
    # internal target can't slip past the IPv4 rules below. We don't rely on
    # stdlib classification of these — it was also buggy before CPython 3.11.10 /
    # 3.12.4 (CVE-2024-4032), so this closes the gap regardless of patch level.
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is not None:
            return _ip_blocked_reason(str(inner))
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
    # Default-deny backstop: anything not globally routable (NAT64, Teredo,
    # benchmarking ranges, future-reserved space the enumerated checks miss) is
    # refused rather than allowed by omission.
    if not ip.is_global:
        return "non-global"
    return None


def check_url(
    url: str, *, allow_private: bool | None = None, allowed_domains: list[str] | None = None
) -> None:
    """Raise BlockedURLError if ``url`` should not be fetched.

    ``allow_private`` None consults the ``ZU_HTTP_ALLOW_PRIVATE`` env var;
    an explicit bool overrides it.

    ``allowed_domains`` (issue #74) is an optional POSITIVE per-agent navigation
    allowlist of wildcard host patterns (``*.example.com``). It is an ADDITIONAL
    gate ON TOP of the SSRF default-deny backstop, NOT a replacement: a host must
    pass the backstop AND match the allowlist. Because callers pass it on every
    redirect hop, an off-allowlist 302 is blocked at the hop, not just the initial
    URL. ``None`` ⇒ no allowlist (the backstop alone governs); ``allow_private``
    does NOT bypass the allowlist (a positive guarantee, even in local dev)."""
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

    # The positive allowlist is enforced regardless of allow_private — it is a
    # per-agent navigation guarantee, not an SSRF backstop that local dev waives.
    _check_allowlist(host, allowed_domains)

    if allow_private:
        return

    # Note: we deliberately do NOT block on port. Aggressive port-blocking would
    # break legitimate public APIs on custom ports, and the private-range guard
    # below already covers the high-value internal targets (metadata, loopback,
    # RFC1918) regardless of which port they listen on.
    for ip in _resolve_ips(host):
        reason = _ip_blocked_reason(ip)
        if reason is not None:
            raise BlockedURLError(
                f"refusing to fetch {host!r} -> {ip} ({reason}); "
                "set ZU_HTTP_ALLOW_PRIVATE=1 to override for local development",
                kind="ssrf",
                target=host,
            )


def validate_and_pin(
    url: str, *, allow_private: bool | None = None, allowed_domains: list[str] | None = None
) -> str | None:
    """Scheme/host check + SSRF validation + pin, resolving the host exactly ONCE.

    A combined ``check_url`` + ``pin_ip`` for callers (e.g. ``render_dom``) that
    need both the backstop *and* a pinned IP: doing them separately resolves the
    host twice, reopening the very DNS-rebinding TOCTOU the pin exists to close
    (the two ``getaddrinfo`` calls can disagree under a low-TTL record). Here a
    single resolution feeds both the validation and the returned pin.

    Returns one validated IP to pin the connection to, or ``None`` when
    ``allow_private`` skips pinning (local dev). Raises ``BlockedURLError`` on a
    bad scheme, a missing host, or any internal/non-global resolved address —
    the same ``kind="ssrf"`` block ``check_url`` raises, so the loop records a
    ``harness.defense.blocked`` event identically."""
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
    # Positive navigation allowlist (issue #74), enforced before the SSRF resolve
    # and regardless of allow_private — same matcher as the pre-exec gate.
    _check_allowlist(host, allowed_domains)
    if allow_private:
        return None
    ips = _resolve_ips(host)  # the single, authoritative resolution
    for ip in ips:
        reason = _ip_blocked_reason(ip)
        if reason is not None:
            raise BlockedURLError(
                f"refusing to fetch {host!r} -> {ip} ({reason}); "
                "set ZU_HTTP_ALLOW_PRIVATE=1 to override for local development",
                kind="ssrf",
                target=host,
            )
    for ip in ips:  # prefer an IPv4 address for the pin (broadest reachability)
        if ":" not in ip:
            return ip
    return next(iter(ips))


def _validated_ips(host: str) -> list[str]:
    """Resolve ``host`` ONCE, validate every address, and return ALL validated IPs
    to try, IPv4 first (broadest reachability) then IPv6. This is the
    *authoritative* resolution: closing the DNS-rebinding TOCTOU means the
    addresses we validate are the addresses we connect to, with no second lookup
    in between. Raises ``BlockedURLError`` if ANY resolved address is internal
    (default-deny: one poisoned answer fails the whole fetch)."""
    ips = _resolve_ips(host)
    for ip in ips:
        reason = _ip_blocked_reason(ip)
        if reason is not None:
            raise BlockedURLError(
                f"refusing to connect to {host!r} -> {ip} ({reason})",
                kind="ssrf",
                target=host,
            )
    v4 = sorted(ip for ip in ips if ":" not in ip)
    v6 = sorted(ip for ip in ips if ":" in ip)
    return v4 + v6


def pin_ip(host: str) -> str:
    """One validated IP to pin a connection to (the first, IPv4-preferred). Kept
    for callers that want a single pin; ``PinnedTransport`` uses ``_validated_ips``
    so it can fall back across a host's endpoints."""
    return _validated_ips(host)[0]


class PinnedTransport(httpx.AsyncBaseTransport):
    """An SSRF-safe httpx transport that closes the DNS-rebinding TOCTOU.

    ``check_url`` validates a host's addresses, but httpx would re-resolve
    independently at connect time — a low-TTL record can answer with a public IP
    on the first lookup and ``169.254.169.254`` on the second, slipping past the
    check. This transport performs the authoritative resolution itself: it
    validates the host and **pins the connection to a validated IP**, while
    preserving the original hostname for the ``Host`` header and the TLS SNI (via
    httpcore's ``sni_hostname`` extension), so certificate validation is against
    the hostname, not the IP. There is no second, unvalidated lookup. When a host
    has several endpoints, an idempotent fetch falls back across the validated IPs
    (each keeps SNI/Host on the hostname) instead of dying on one bad endpoint.
    ``allow_private`` (None ⇒ ``ZU_HTTP_ALLOW_PRIVATE``) skips pinning for local
    development."""

    def __init__(
        self,
        *,
        allow_private: bool | None = None,
        inner: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._allow_private = allow_private
        self._inner = inner or httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        allow_private = self._allow_private
        if allow_private is None:
            allow_private = os.environ.get("ZU_HTTP_ALLOW_PRIVATE") == "1"
        host = request.url.host
        if allow_private or not host or request.url.scheme not in _ALLOWED_SCHEMES:
            return await self._inner.handle_async_request(request)

        ips = _validated_ips(host)  # authoritative resolve+validate, raises if internal
        # Keep the original hostname for TLS SNI + cert validation; the Host header
        # httpx already set from the original URL stays the hostname too.
        request.extensions = {**request.extensions, "sni_hostname": host}
        original = request.url
        # A host often resolves to several CDN/WAF endpoints; if the pinned one
        # fails the handshake (the cert-vs-endpoint case) or is unreachable, fall
        # back across the rest rather than failing the fetch. Only an idempotent
        # request is retried — a body may not be re-sendable; http_fetch is GET.
        candidates = ips if request.method in ("GET", "HEAD") else ips[:1]
        last_exc: Exception | None = None
        for ip in candidates:
            request.url = original.copy_with(host=ip)
            try:
                return await self._inner.handle_async_request(request)
            except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                last_exc = exc  # connect/TLS failure before any response byte
        raise last_exc if last_exc is not None else RuntimeError("no validated IP")

    async def aclose(self) -> None:
        await self._inner.aclose()
