"""The SSRF host/IP guard — "is this host or address internal?" — stdlib only.

This is the single hardened answer to the one question the egress proxy
(``egress_proxy.py``) and the red-team verdict (``zu_redteam.verdict``) both ask:
*may a plugin ever reach this host?* Keeping it here, behind one function, stops
the proxy's guard and the judge's guard from drifting apart — a drift that would
let a metadata-SSRF reach pass one gate while the other refuses it.

Two failure classes this hardens against, both encoding tricks libc-backed
clients (curl/requests) accept that ``ipaddress.ip_address`` rejects verbatim:

* **Encoded IPv4** — a single decimal integer (``2130706433`` = ``127.0.0.1``),
  ``0x``-hex (``0x7f000001``), octal dotted-quad (``0177.0.0.1``), and mixed
  forms. Each is canonicalised to a structural IP and classified, so every
  spelling of a link-local / loopback / private address is caught structurally,
  not by a denylist of literals.
* **Trailing-dot / bare metadata names** — ``metadata.google.internal.`` (a fully
  valid FQDN with the root dot) and the bare ``metadata`` label. A single
  trailing dot is stripped before the name comparison.

The internal-range detection is generic: loopback / link-local (which is where
the ``169.254.169.254`` cloud-metadata address lives — caught by classification,
not a magic constant) / private (RFC1918) / unique-local (``fd00::/8``) /
reserved / unspecified, plus IPv6 forms that embed an IPv4 address.

This module is pure stdlib (``ipaddress``), so both plugin packages can carry the
same logic with no new cross-package dependency edge; a parity test pins the two
copies together (``packages/zu-redteam/tests/test_verdict.py``).
"""

from __future__ import annotations

import ipaddress

# A parsed address is always one of these concrete types; the abstract
# ``_BaseAddress`` does not expose the ``is_*`` classification properties.
_IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address

# Well-known internal *names* no plugin may reach. These are a defence-in-depth
# convenience for the name forms; the load-bearing check is structural IP
# classification (so 169.254.169.254 is caught by link-local detection whether it
# arrives as an IP or behind one of these names resolving to it).
_INTERNAL_NAMES = frozenset(
    {
        "localhost",
        # Cloud-metadata service names across the major providers. The address
        # itself (169.254.169.254 / 100.100.100.200) is caught structurally; these
        # cover the *name* spellings a CONNECT/request line may carry directly.
        "metadata",
        "metadata.google.internal",
        "metadata.goog",
        "metadata.azure.com",
        "instance-data",
        "instance-data.ec2.internal",
    }
)


def canonical_ip(host: str) -> _IPAddress | None:
    """Parse ``host`` as an IP address, canonicalising the encoded IPv4 forms that
    libc accepts but ``ipaddress.ip_address`` rejects, or return ``None`` for a
    real DNS name.

    Handles: canonical dotted-quad / IPv6; a single decimal integer
    (``2130706433``); ``0x``-hex (``0x7f000001``); octal dotted-quad
    (``0177.0.0.1``); and mixed-radix dotted forms (``0x7f.0.0.1``). Any address
    that ``ipaddress`` already parses is returned as-is.
    """
    text = (host or "").strip()
    if not text:
        return None
    # Canonical forms (dotted-quad IPv4, IPv6, IPv4-in-IPv6) first.
    try:
        return ipaddress.ip_address(text)
    except ValueError:
        pass
    # A bracketed IPv6 literal (``[::1]``) as it can appear in a host field.
    if text.startswith("[") and text.endswith("]"):
        try:
            return ipaddress.ip_address(text[1:-1])
        except ValueError:
            return None
    # IPv4 forms libc accepts but the canonical parser does not: a single integer,
    # or a dotted address whose octets are hex/octal/decimal. inet_aton-style.
    parts = text.split(".")
    try:
        if len(parts) == 1:
            value = _parse_int_octet(parts[0])
            if value is None or value > 0xFFFFFFFF:
                return None
            return ipaddress.IPv4Address(value)
        if len(parts) == 4:
            octets = [_parse_int_octet(p) for p in parts]
            if any(o is None or o > 0xFF for o in octets):
                return None
            value = 0
            for o in octets:
                value = (value << 8) | o  # type: ignore[operator]
            return ipaddress.IPv4Address(value)
    except (ValueError, ipaddress.AddressValueError):
        return None
    return None


def _parse_int_octet(token: str) -> int | None:
    """Parse one IPv4 octet/integer in decimal, ``0x``-hex, or leading-zero octal
    (the radixes ``inet_aton`` honours). Returns ``None`` on anything non-numeric."""
    token = token.strip()
    if not token:
        return None
    try:
        low = token.lower()
        if low.startswith("0x"):
            return int(token, 16)
        if token.startswith("0") and token != "0":
            return int(token, 8)
        return int(token, 10)
    except ValueError:
        return None


def is_internal_ip(ip: _IPAddress) -> bool:
    """Classify a parsed IP as internal: loopback / link-local (incl. cloud
    metadata 169.254.169.254) / private (RFC1918) / unique-local (``fd00::/8``) /
    reserved / unspecified. IPv6 forms that embed an IPv4 address are unwrapped and
    re-checked so a mapped/tunnelled internal target can't slip past."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is not None:
            return is_internal_ip(inner)
    return bool(
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_reserved
        or ip.is_unspecified
    )


def is_internal_host(host: str) -> bool:
    """A host no plugin may ever reach. ``True`` for any internal/metadata target
    expressed as a literal IP (any encoding) or as a well-known internal name.

    Names are *not* resolved here (DNS resolution is the dial-time guard's job in
    the proxy, and the verdict deliberately does not resolve) — but every encoded
    spelling of an internal IP is normalised structurally, and a single trailing
    dot is stripped before the name comparison so ``metadata.google.internal.``
    is treated identically to ``metadata.google.internal``.
    """
    lowered = (host or "").strip().lower()
    if lowered.endswith(".") and not lowered.endswith(".."):
        lowered = lowered[:-1]  # strip a single trailing (root) dot
    if lowered in _INTERNAL_NAMES:
        return True
    ip = canonical_ip(lowered)
    if ip is None:
        return False
    return is_internal_ip(ip)
