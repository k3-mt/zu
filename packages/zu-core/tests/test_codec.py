"""The payload codec seam: tag-then-body, version dispatch, mixed-codec reads."""

from __future__ import annotations

import pytest

from zu_core.codec import IdentityCodec, PayloadCodec, decode_payload, encode_payload


def test_identity_roundtrip() -> None:
    codec = IdentityCodec()
    registry = {0: codec}
    blob = encode_payload(codec, '{"k": "v"}', aad=b"event-1")
    assert blob[0] == 0  # version tag
    assert decode_payload(blob, b"event-1", registry) == '{"k": "v"}'


def test_unknown_version_raises() -> None:
    blob = bytes([9]) + b"whatever"
    with pytest.raises(ValueError, match="no codec registered"):
        decode_payload(blob, b"", {0: IdentityCodec()})


def test_empty_blob_raises() -> None:
    with pytest.raises(ValueError, match="empty payload"):
        decode_payload(b"", b"", {0: IdentityCodec()})


def test_mixed_codec_registry_reads_old_rows() -> None:
    # A registry can hold several codecs; each blob decodes by its own tag.
    class FakeCipher:
        version = 7

        def encode_body(self, plaintext: str, aad: bytes) -> bytes:
            return plaintext.encode("utf-8")[::-1]  # toy "encryption"

        def decode_body(self, body: bytes, aad: bytes) -> str:
            return body[::-1].decode("utf-8")

    identity, cipher = IdentityCodec(), FakeCipher()
    registry: dict[int, PayloadCodec] = {0: identity, 7: cipher}

    plain_row = encode_payload(identity, "old plaintext", b"a")
    cipher_row = encode_payload(cipher, "new payload", b"b")

    assert decode_payload(plain_row, b"a", registry) == "old plaintext"
    assert decode_payload(cipher_row, b"b", registry) == "new payload"
