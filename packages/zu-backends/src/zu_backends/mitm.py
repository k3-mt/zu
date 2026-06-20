"""MitmCA — a per-run MITM certificate authority for the egress proxy (P2).

The P1 proxy sees only the CONNECT *host* of an HTTPS request — enough for the
egress allowlist, but the URL/query/body are inside the TLS tunnel, so a secret
smuggled into an HTTPS query string is invisible. P2 lets the proxy terminate TLS
with a leaf cert this CA mints on demand, read the plaintext request (to spot the
planted secret), and re-originate TLS upstream. ``ExfilBreach`` already judges the
payload the proxy records (RED_TEAM.md §3); this is what lets the proxy record it.

The CA is generated **per run** and is meant to be trusted ONLY inside the
ephemeral target container — it never touches the host trust store, and dies with
the run. It is the one new sharp edge of the gate (RED_TEAM_CONTAINER.md §7), so
it is deliberately small and short-lived.

Requires the ``cryptography`` extra (``zu-backends[encryption]``); imported lazily
by the proxy so the stdlib egress path never needs it.
"""

from __future__ import annotations

import datetime
import ipaddress
import os
import ssl
import tempfile
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

_DAY = datetime.timedelta(days=1)


class MitmCA:
    """A short-lived CA that mints server leaf certs on demand and hands back a
    server-side ``ssl.SSLContext`` per host (cached)."""

    def __init__(self, common_name: str = "Zu Red-Team MITM CA") -> None:
        self._ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
        now = datetime.datetime.now(datetime.UTC)
        self._ca_cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self._ca_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _DAY)
            .not_valid_after(now + _DAY)
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(self._ca_key, hashes.SHA256())
        )
        self._ctx_cache: dict[str, ssl.SSLContext] = {}

    def ca_cert_pem(self) -> bytes:
        """The CA certificate (PEM) to install in the target container's trust
        store so the in-container client trusts the proxy's minted leaves."""
        return self._ca_cert.public_bytes(serialization.Encoding.PEM)

    def _mint(self, host: str) -> tuple[bytes, bytes]:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.datetime.now(datetime.UTC)
        try:
            san: Any = x509.IPAddress(ipaddress.ip_address(host))
        except ValueError:
            san = x509.DNSName(host)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
            .issuer_name(self._ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - _DAY)
            .not_valid_after(now + _DAY)
            .add_extension(x509.SubjectAlternativeName([san]), critical=False)
            .sign(self._ca_key, hashes.SHA256())
        )
        return (
            cert.public_bytes(serialization.Encoding.PEM),
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.TraditionalOpenSSL,
                serialization.NoEncryption(),
            ),
        )

    def leaf_context(self, host: str) -> ssl.SSLContext:
        """A server-side TLS context presenting a freshly minted leaf for ``host``
        (cached per host). The proxy uses it to impersonate the upstream to the
        in-container client."""
        cached = self._ctx_cache.get(host)
        if cached is not None:
            return cached
        cert_pem, key_pem = self._mint(host)
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # load_cert_chain reads from a path; write the cert+key to one temp file,
        # load it, and remove it immediately (the context keeps the loaded material).
        fd, path = tempfile.mkstemp(suffix=".pem")
        try:
            os.write(fd, cert_pem + b"\n" + key_pem)
            os.close(fd)
            ctx.load_cert_chain(path)
        finally:
            os.unlink(path)
        self._ctx_cache[host] = ctx
        return ctx
