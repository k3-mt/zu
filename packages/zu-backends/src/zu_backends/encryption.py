"""AES-256-GCM payload codecs — encryption-at-rest behind the optional extra.

Install with ``zu-backends[encryption]`` (pulls in ``cryptography``). Pass a
codec to a durable sink to encrypt event payloads at rest. Two codecs ship:

* ``AesGcmCodec`` (version 1) — a single 32-byte key. The simplest form.
* ``ManagedAesGcmCodec`` (version 2) — keys come from a :class:`KeyProvider`, so
  keys can **rotate** and be sourced from a KMS of the deployment's choice. Each
  blob records the **key id** it was written under, so old rows keep decrypting
  after a rotation. This is the recommended form for a regulated deployment::

      from zu_backends.sqlite_sink import SqliteSink
      from zu_backends.encryption import ManagedAesGcmCodec
      sink = SqliteSink("zu.db", codec=ManagedAesGcmCodec.from_env())

Blob layout — v1: ``[ver=1][nonce][ct+tag]``; v2: ``[ver=2][kid_len][kid][nonce]
[ct+tag]``. The associated data (AAD) binds the row's indexed columns (event_id,
trace_id, task_id, type, source), so tampering with any plaintext index column
makes the row fail to decrypt — it cannot be silently edited to hide a record
from a filter. Only the payload is encrypted; the index columns stay plaintext
so the log remains queryable.

Key management: ``AesGcmCodec`` takes raw key bytes; ``ManagedAesGcmCodec`` takes
a ``KeyProvider`` (default: :class:`EnvKeyProvider`). The **KMS choice is the
deployment's** — implement ``KeyProvider`` against AWS KMS / GCP KMS / Vault and
pass it in; nothing here is baked to a vendor.
"""

from __future__ import annotations

import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from zu_core.codec import KeyProvider

_NONCE_LEN = 12
_KEY_LEN = 32  # AES-256


class AesGcmCodec:
    version = 1

    def __init__(self, key: bytes) -> None:
        if len(key) != _KEY_LEN:
            raise ValueError(f"AES-256-GCM needs a {_KEY_LEN}-byte key, got {len(key)}")
        self._aes = AESGCM(key)

    @classmethod
    def from_env(cls, var: str = "ZU_EVENT_KEY") -> AesGcmCodec:
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


class EnvKeyProvider:
    """A :class:`KeyProvider` that reads keys from the environment — the default,
    zero-infrastructure managed-key source, and a model to copy for a real KMS.

    Keys live in ``ZU_EVENT_KEY_<id>`` env vars (hex or base64, 32 bytes), and
    ``ZU_EVENT_KEY_ID`` names the current one for new writes. To rotate: add a new
    key under a new id, point ``ZU_EVENT_KEY_ID`` at it, and keep the old vars so
    existing rows still decrypt. For back-compat, the bare ``ZU_EVENT_KEY`` is the
    key for id ``"default"`` (the id used when ``ZU_EVENT_KEY_ID`` is unset)."""

    _PREFIX = "ZU_EVENT_KEY_"
    _LEGACY = "ZU_EVENT_KEY"
    _DEFAULT_ID = "default"

    def __init__(self, current_key_id: str | None = None) -> None:
        self._current = current_key_id or os.environ.get("ZU_EVENT_KEY_ID", self._DEFAULT_ID)

    @classmethod
    def from_env(cls) -> EnvKeyProvider:
        return cls()

    @property
    def current_key_id(self) -> str:
        return self._current

    def key(self, key_id: str) -> bytes:
        raw = os.environ.get(self._PREFIX + key_id)
        if raw is None and key_id == self._DEFAULT_ID:
            raw = os.environ.get(self._LEGACY)  # back-compat: bare ZU_EVENT_KEY
        if not raw:
            raise RuntimeError(
                f"no key for id {key_id!r}: set {self._PREFIX}{key_id} (a 32-byte "
                "hex/base64 key). After rotating, keep old keys so old rows decrypt."
            )
        return _decode_key(raw)


class ManagedAesGcmCodec:
    """AES-256-GCM keyed by a :class:`KeyProvider`, with the key id embedded per
    blob so keys rotate without losing readability of older rows."""

    version = 2

    def __init__(self, key_provider: KeyProvider) -> None:
        self._kp = key_provider

    @classmethod
    def from_env(cls) -> ManagedAesGcmCodec:
        return cls(EnvKeyProvider.from_env())

    def _aes(self, key_id: str) -> AESGCM:
        key = self._kp.key(key_id)
        if len(key) != _KEY_LEN:
            raise ValueError(f"AES-256-GCM needs a {_KEY_LEN}-byte key, got {len(key)}")
        return AESGCM(key)

    def encode_body(self, plaintext: str, aad: bytes) -> bytes:
        kid = self._kp.current_key_id
        kid_b = kid.encode("utf-8")
        if not 0 < len(kid_b) <= 255:
            raise ValueError(f"key id must be 1..255 UTF-8 bytes, got {len(kid_b)}")
        nonce = os.urandom(_NONCE_LEN)
        ct = self._aes(kid).encrypt(nonce, plaintext.encode("utf-8"), _bind_kid(aad, kid_b))
        return bytes([len(kid_b)]) + kid_b + nonce + ct

    def decode_body(self, body: bytes, aad: bytes) -> str:
        klen = body[0]
        kid_b = body[1 : 1 + klen]
        kid = kid_b.decode("utf-8")
        rest = body[1 + klen :]
        nonce, ct = rest[:_NONCE_LEN], rest[_NONCE_LEN:]
        # ``kid`` is bound into the AAD so the key id recorded in the blob is
        # authenticated: an at-rest attacker who rewrites it to point at a
        # different (weaker/known) key makes the row fail to decrypt rather than
        # silently re-key it.
        return self._aes(kid).decrypt(nonce, ct, _bind_kid(aad, kid_b)).decode("utf-8")


def _bind_kid(aad: bytes, kid_b: bytes) -> bytes:
    """The effective GCM AAD for a v2 blob: the row's index columns plus a
    length-framed key id, so the embedded ``kid`` is authenticated alongside the
    plaintext columns. Length-framing keeps ``aad``/``kid`` unambiguous."""
    return aad + bytes([len(kid_b)]) + kid_b


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
