"""AES-256-GCM payload codec — encryption-at-rest behind the optional extra.

Install with ``zu-backends[encryption]`` (pulls in ``cryptography``). Pass it to
a durable sink to encrypt event payloads at rest:

    from zu_backends.sqlite_sink import SqliteSink
    from zu_backends.encryption import AesGcmCodec
    sink = SqliteSink("zu.db", codec=AesGcmCodec.from_env())

Each blob is ``[version=1][12-byte nonce][AES-256-GCM ciphertext+tag]``, with
the row's event_id bound as associated data (AAD) so a ciphertext can't be
replayed into another row. Only the payload is encrypted; the indexed metadata
columns (ids, type, source, seq) stay plaintext so the log remains queryable.

Key management here is deliberately minimal — a 32-byte key from the
environment. Managed keys (KMS, envelope encryption, rotation) are a future
stage: this codec asks only for raw key bytes, so a KeyProvider that fetches a
KMS-wrapped data key slots in without touching the on-disk format.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_NONCE_LEN = 12
_KEY_LEN = 32  # AES-256


class AesGcmCodec:
    version = 1

    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_LEN:
            raise ValueError(f"AES-256-GCM needs a {_KEY_LEN}-byte key, got {len(key)}")
        self._aes = AESGCM(key)

    @classmethod
    def from_env(cls, var: str = "ZU_EVENT_KEY") -> "AesGcmCodec":
        """Build from a base64/hex 32-byte key in the environment."""
        raw = os.environ.get(var)
        if not raw:
            raise RuntimeError(
                f"{var} is not set; provide a 32-byte key (hex or base64) to "
                "enable encryption-at-rest, or use the default plaintext codec."
            )
        key = _decode_key(raw)
        return cls(key)

    def encode_body(self, plaintext: str, aad: bytes) -> bytes:
        nonce = os.urandom(_NONCE_LEN)
        ct = self._aes.encrypt(nonce, plaintext.encode("utf-8"), aad)
        return nonce + ct

    def decode_body(self, body: bytes, aad: bytes) -> str:
        nonce, ct = body[:_NONCE_LEN], body[_NONCE_LEN:]
        return self._aes.decrypt(nonce, ct, aad).decode("utf-8")


def _decode_key(raw: str) -> bytes:
    import base64
    import binascii

    raw = raw.strip()
    # try hex first, then base64
    try:
        if len(raw) == _KEY_LEN * 2:
            return bytes.fromhex(raw)
    except ValueError:
        pass
    try:
        return base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("ZU_EVENT_KEY must be a 32-byte key as hex or base64") from exc
