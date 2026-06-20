"""Payload codec seam — the encryption-at-rest boundary for durable sinks.

Encryption-at-rest is deferred as a *cipher* but not as a *format*: an
append-only log is the worst place to retrofit encryption (you accumulate
immutable plaintext), so the on-disk envelope is fixed now and the cipher is
swappable later. Every stored payload blob begins with a one-byte **version
tag** identifying the codec that wrote it, so a log can hold rows written by
different codecs (e.g. plaintext rows from before encryption was enabled) and
still be read back — the durable sink decodes each row by its own tag.

Default is `IdentityCodec` (plaintext, zero dependencies). A real AES-256-GCM
codec ships behind zu-backends' optional ``[encryption]`` extra. The AES codec
binds the row's indexed columns as associated data (AAD), so a ciphertext can't
be moved to — or have its index columns edited on — a different row. The default
`IdentityCodec` is plaintext and provides *no* integrity: it accepts the ``aad``
argument for interface parity but cannot bind it (there is no authentication tag
over plaintext), so the move-resistance guarantee applies only once a cipher is
configured. Managed keys (KMS / envelope encryption / rotation) are a future
stage; the codec asks for a key, so swapping an env-var key for a KMS provider
later is a contained change with no on-disk format impact.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable


@runtime_checkable
class PayloadCodec(Protocol):
    version: int  # 0-255; the tag byte written as the first byte of each blob

    def encode_body(self, plaintext: str, aad: bytes) -> bytes: ...

    def decode_body(self, body: bytes, aad: bytes) -> str: ...


@runtime_checkable
class KeyProvider(Protocol):
    """Supplies symmetric data keys *by id*, so a codec can rotate keys and a
    deployment can source them from the KMS/secret store of its choice (AWS KMS,
    GCP KMS, Vault, an HSM, …) — the choice belongs to whoever runs Zu, never
    baked in here. The codec never holds a long-lived master key: it asks the
    provider for the *current* key id when writing, and for a specific key id
    (read back off the stored blob) when decrypting an older row.

    Key rotation is the answer to AES-GCM's nonce-scaling bound too: a fresh
    random 96-bit nonce is safe to ~2^32 events under one key, so rotating the
    data key (a new ``current_key_id``) resets that budget while old rows keep
    decrypting under their own key id. Implement this against a KMS to get
    managed keys with no on-disk format change."""

    @property
    def current_key_id(self) -> str: ...

    def key(self, key_id: str) -> bytes: ...


class IdentityCodec:
    """Plaintext. The default: no dependencies, fully queryable on disk.

    ``aad`` is accepted for interface parity with authenticated codecs but is
    intentionally unused: plaintext carries no authentication tag, so there is
    nothing to bind it to. The AAD row-binding guarantee is a property of the
    AES codec only — see the module docstring.
    """

    version = 0

    def encode_body(self, plaintext: str, aad: bytes) -> bytes:
        return plaintext.encode("utf-8")

    def decode_body(self, body: bytes, aad: bytes) -> str:
        return body.decode("utf-8")


def encode_payload(codec: PayloadCodec, plaintext: str, aad: bytes = b"") -> bytes:
    """Tag-then-body: [version byte][codec-specific body]."""
    if not 0 <= codec.version <= 255:
        raise ValueError(f"codec.version must be a byte (0-255), got {codec.version}")
    return bytes([codec.version]) + codec.encode_body(plaintext, aad)


def decode_payload(
    blob: bytes, aad: bytes, registry: Mapping[int, PayloadCodec]
) -> str:
    """Dispatch on the leading version byte so mixed-codec logs read back."""
    if not blob:
        raise ValueError("empty payload blob")
    version = blob[0]
    codec = registry.get(version)
    if codec is None:
        raise ValueError(
            f"no codec registered for payload version {version}; "
            "cannot decode (was this row written with an encryption codec "
            "that is not installed/configured?)"
        )
    return codec.decode_body(blob[1:], aad)
