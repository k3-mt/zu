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

from zu_core.contracts import Event  # noqa: E402
from zu_backends.encryption import AesGcmCodec  # noqa: E402
from zu_backends.sqlite_sink import SqliteSink  # noqa: E402

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


async def test_sink_roundtrip_with_encryption() -> None:
    sink = SqliteSink(":memory:", codec=AesGcmCodec(_KEY))
    ev = _event(uuid4())
    await sink.append(ev)
    assert await sink.query({"task_id": ev.task_id}) == [ev]  # decrypts cleanly


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
