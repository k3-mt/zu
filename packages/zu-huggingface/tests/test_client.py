"""The HfClient seam (§6.4) — both backends satisfy the one contract, the pure
helpers normalise to the shared shapes, and the tabular local branch refuses
rather than fetch a model. Offline: no network, no model download.

The PipelineBackend's real task methods are never *called* here (that would need
transformers + a download); what is proven is (a) both backends are structural
HfClients, (b) the pure normalisers/encoders, and (c) the hosted-only refusal.
"""

from __future__ import annotations

import base64
import struct
import wave

import pytest

from zu_huggingface import InferenceClientBackend, PipelineBackend
from zu_huggingface.client import (
    HfClient,
    _qa_top,
    _scores,
    _segments,
    _wav_bytes,
)


def test_both_backends_are_hfclients() -> None:
    # runtime_checkable structural check — the Protocol additions keep both
    # backends satisfying HfClient (so a tool works hosted or local, and an
    # isinstance gate can't silently drift).
    assert isinstance(InferenceClientBackend(), HfClient)
    assert isinstance(PipelineBackend(), HfClient)


def test_segments_normaliser_attr_and_dict_shapes() -> None:
    # accepts both attribute-style HF output elements and plain dicts; masks are
    # PNG-base64 encoded (bytes -> b64), never raw bytes in the observation.
    class _El:
        label = "cat"
        score = 0.9
        mask = b"\x89PNGmask"

    out = _segments([_El(), {"label": "bg", "mask": b"x"}])
    assert out[0]["label"] == "cat" and out[0]["score"] == 0.9
    assert base64.b64decode(out[0]["mask_b64"]) == b"\x89PNGmask"
    assert "score" not in out[1]  # omitted when absent


def test_qa_top_normalises_list_and_dict() -> None:
    assert _qa_top([{"answer": "x", "score": 0.5}]) == {"answer": "x", "score": 0.5}
    assert _qa_top({"answer": "y"}) == {"answer": "y"}
    assert _qa_top([]) == {"answer": ""}


def test_audio_classification_reuses_scores_normaliser() -> None:
    # the same normaliser the text classifier uses → audio classifier output is
    # interchangeable with the classifier contract (label/score sorted desc).
    out = _scores([{"label": "music", "score": 0.2}, {"label": "speech", "score": 0.8}])
    assert out[0] == {"label": "speech", "score": 0.8}


def test_wav_bytes_encodes_a_valid_wav() -> None:
    # the local-TTS encoder, fed a tiny synthetic float sample list (no numpy /
    # no model). Proves the bytes contract holds on the local surface too.
    samples = [0.0, 0.5, -0.5, 1.0]
    blob = _wav_bytes(samples, 8000)
    assert blob[:4] == b"RIFF" and blob[8:12] == b"WAVE"
    with wave.open(__import__("io").BytesIO(blob), "rb") as w:
        assert w.getnchannels() == 1
        assert w.getsampwidth() == 2
        assert w.getframerate() == 8000
        frames = w.readframes(w.getnframes())
    assert struct.unpack("<h", frames[2:4])[0] == int(0.5 * 32767)


def test_tabular_local_is_hosted_only_no_model_fetch() -> None:
    # the local tabular branch raises a clear hosted-only error — it never builds
    # a pipeline, so it cannot bypass the supply-chain guard.
    be = PipelineBackend()
    with pytest.raises(RuntimeError, match="hosted-only"):
        be.tabular_classification({"x": ["1"]}, "acme/m")
    with pytest.raises(RuntimeError, match="hosted-only"):
        be.tabular_regression({"x": ["1"]}, "acme/m")
