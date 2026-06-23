"""The AES-256-GCM payload codec — round-trip, AAD binding, on-disk ciphertext.

`cryptography` is an optional runtime extra; it's in the dev group so this runs
in CI. Skipped if it isn't installed.
"""

from __future__ import annotations

import os
from uuid import uuid4

import pytest

pytest.importorskip("cryptography")

from cryptography.exceptions import InvalidTag  # noqa: E402

from zu_backends.encryption import (  # noqa: E402
    AesGcmCodec,
    EnvKeyProvider,
    ManagedAesGcmCodec,
)
from zu_backends.sqlite_sink import SqliteSink  # noqa: E402
from zu_core.contracts import Event  # noqa: E402

_KEY = os.urandom(32)


def _event(task_id) -> Event:
    return Event(
        trace_id=uuid4(),
        task_id=task_id,
        type="data.source.fetched",
        source="loop",
        payload={"secret": "patient-record-12345"},
    )


def test_codec_roundtrip() -> None:
    codec = AesGcmCodec(_KEY)
    body = codec.encode_body("hello", aad=b"event-1")
    assert codec.decode_body(body, aad=b"event-1") == "hello"


def test_aad_mismatch_fails() -> None:
    codec = AesGcmCodec(_KEY)
    body = codec.encode_body("hello", aad=b"event-1")
    with pytest.raises(InvalidTag):
        codec.decode_body(body, aad=b"event-2")  # wrong row -> rejected


def test_bad_key_length() -> None:
    with pytest.raises(ValueError):
        AesGcmCodec(b"too-short")


def test_tampered_ciphertext_is_rejected() -> None:
    # Flipping a single bit of the stored blob must fail the GCM tag check, so a
    # tampered-at-rest payload is rejected, not silently decrypted to garbage.
    codec = AesGcmCodec(_KEY)
    body = bytearray(codec.encode_body("hello", aad=b"event-1"))
    body[-1] ^= 0x01  # corrupt the last byte (inside the auth tag)
    with pytest.raises(InvalidTag):
        codec.decode_body(bytes(body), aad=b"event-1")


async def test_sink_roundtrip_with_encryption() -> None:
    sink = SqliteSink(":memory:", codec=AesGcmCodec(_KEY))
    ev = _event(uuid4())
    stored = await sink.append(ev)  # linked + encrypted
    assert await sink.query({"task_id": ev.task_id}) == [stored]  # decrypts cleanly


async def test_payload_is_ciphertext_on_disk(tmp_path) -> None:
    db = str(tmp_path / "enc.db")
    sink = SqliteSink(db, codec=AesGcmCodec(_KEY))
    ev = _event(uuid4())
    await sink.append(ev)
    sink.close()

    # Read the raw file: the sensitive value must not appear in plaintext.
    raw = (tmp_path / "enc.db").read_bytes()
    assert b"patient-record-12345" not in raw


def test_from_env(monkeypatch) -> None:
    monkeypatch.setenv("ZU_EVENT_KEY", _KEY.hex())
    codec = AesGcmCodec.from_env()
    body = codec.encode_body("x", aad=b"a")
    assert codec.decode_body(body, aad=b"a") == "x"


# --- managed keys: KeyProvider + rotation (version 2) ------------------------


class _DictKeyProvider:
    """A toy KeyProvider — the shape a real KMS-backed one would implement."""

    def __init__(self, keys: dict, current: str) -> None:
        self._keys = keys
        self._current = current

    @property
    def current_key_id(self) -> str:
        return self._current

    def key(self, key_id: str) -> bytes:
        return self._keys[key_id]


def test_managed_codec_roundtrip_and_embeds_key_id() -> None:
    kp = _DictKeyProvider({"k1": os.urandom(32)}, current="k1")
    codec = ManagedAesGcmCodec(kp)
    body = codec.encode_body("hello", aad=b"e1")
    # The key id is recorded in the blob (so an old row knows which key decrypts it).
    klen = body[0]
    assert body[1 : 1 + klen] == b"k1"
    assert codec.decode_body(body, aad=b"e1") == "hello"


def test_tampered_key_id_is_rejected() -> None:
    # The embedded key id is bound into the AAD, so an at-rest attacker who
    # rewrites it (to point a row at a different/weaker key) makes the row fail
    # to decrypt rather than silently re-key it.
    keys = {"k1": os.urandom(32), "k2": os.urandom(32)}
    codec = ManagedAesGcmCodec(_DictKeyProvider(keys, current="k1"))
    body = bytearray(codec.encode_body("secret", aad=b"e1"))
    klen = body[0]
    assert bytes(body[1 : 1 + klen]) == b"k1"
    body[1 : 1 + klen] = b"k2"  # forge the key id to another valid, known key
    with pytest.raises(InvalidTag):
        codec.decode_body(bytes(body), aad=b"e1")


def test_rotation_old_rows_still_decrypt_new_rows_use_new_key() -> None:
    keys = {"k1": os.urandom(32), "k2": os.urandom(32)}
    old = ManagedAesGcmCodec(_DictKeyProvider(keys, current="k1"))
    old_blob = old.encode_body("old-row", aad=b"e1")

    # Rotate: current is now k2, but k1 is retained so old rows still read.
    rotated = ManagedAesGcmCodec(_DictKeyProvider(keys, current="k2"))
    assert rotated.decode_body(old_blob, aad=b"e1") == "old-row"   # old row, old key
    new_blob = rotated.encode_body("new-row", aad=b"e1")
    assert new_blob[1 : 1 + new_blob[0]] == b"k2"                   # new row, new key
    assert rotated.decode_body(new_blob, aad=b"e1") == "new-row"


def test_env_key_provider_rotation(monkeypatch) -> None:
    monkeypatch.setenv("ZU_EVENT_KEY_k1", os.urandom(32).hex())
    monkeypatch.setenv("ZU_EVENT_KEY_k2", os.urandom(32).hex())
    monkeypatch.setenv("ZU_EVENT_KEY_ID", "k2")
    kp = EnvKeyProvider.from_env()
    assert kp.current_key_id == "k2"
    assert len(kp.key("k1")) == 32 and len(kp.key("k2")) == 32


def test_env_key_provider_back_compat_default_id(monkeypatch) -> None:
    # The bare ZU_EVENT_KEY is the 'default' key id.
    monkeypatch.delenv("ZU_EVENT_KEY_ID", raising=False)
    monkeypatch.setenv("ZU_EVENT_KEY", _KEY.hex())
    kp = EnvKeyProvider.from_env()
    assert kp.current_key_id == "default"
    assert kp.key("default") == _KEY


async def test_sink_roundtrip_with_managed_codec() -> None:
    kp = _DictKeyProvider({"k1": os.urandom(32)}, current="k1")
    sink = SqliteSink(":memory:", codec=ManagedAesGcmCodec(kp))
    ev = _event(uuid4())
    stored = await sink.append(ev)
    assert await sink.query({"task_id": ev.task_id}) == [stored]


async def test_tampered_index_column_fails_to_decrypt(tmp_path) -> None:
    # An attacker with raw DB write access edits a plaintext index column to hide
    # a row from a `type` filter. Because the index tuple is bound as AAD, the
    # row now fails to decrypt — the tampering is loud, not silent.
    db = str(tmp_path / "enc.db")
    sink = SqliteSink(db, codec=AesGcmCodec(_KEY))
    ev = _event(uuid4())
    await sink.append(ev)
    sink.close()

    import sqlite3

    conn = sqlite3.connect(db)
    conn.execute("UPDATE events SET type = 'harness.hidden' WHERE event_id = ?", (str(ev.event_id),))
    conn.commit()
    conn.close()

    reopened = SqliteSink(db, codec=AesGcmCodec(_KEY))
    with pytest.raises(InvalidTag):
        await reopened.query({"task_id": ev.task_id})
