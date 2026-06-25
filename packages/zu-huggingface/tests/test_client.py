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
    _depth_magnitudes,
    _qa_top,
    _scores,
    _segments,
    _to_nested_floats,
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


class _FakeTensor:
    """A minimal stand-in for a torch tensor: a leading singleton batch dim that
    ``squeeze`` collapses, plus ``detach``/``cpu``/``tolist`` — so the normaliser is
    exercised without torch/numpy installed."""

    def __init__(self, nested: list) -> None:
        self._nested = nested

    def detach(self) -> _FakeTensor:
        return self

    def cpu(self) -> _FakeTensor:
        return self

    def squeeze(self) -> _FakeTensor:
        # collapse a leading singleton dim, like tensor.squeeze()
        n = self._nested
        while len(n) == 1 and n and isinstance(n[0], list):
            n = n[0]
        return _FakeTensor(n)

    def tolist(self) -> list:
        return self._nested


def test_depth_magnitudes_normalises_a_raw_tensor() -> None:
    # A raw [1, H, W] tensor (the predicted_depth) is normalised to a JSON-safe 2-D
    # grid of floats + min/max so a consumer can recover real distances.
    raw = _FakeTensor([[[0.5, 1.0], [2.0, 4.0]]])  # leading batch dim
    grid = _to_nested_floats(raw)
    assert grid == [[0.5, 1.0], [2.0, 4.0]]
    mags = _depth_magnitudes(raw)
    assert mags == {"depth": [[0.5, 1.0], [2.0, 4.0]], "depth_min": 0.5, "depth_max": 4.0}


def test_depth_magnitudes_absent_when_no_raw_depth() -> None:
    # A backend that returns only the visualisation (no predicted_depth) yields an
    # empty block — additive, so depth_png_b64 alone is never broken.
    assert _depth_magnitudes(None) == {}
    assert _to_nested_floats(None) is None


class _CapturingInferenceClient:
    """A stub for ``huggingface_hub.InferenceClient`` that captures the
    ``chat_completion`` arguments instead of calling the network."""

    def __init__(self) -> None:
        self.captured: dict[str, object] = {}

    def chat_completion(self, *, messages: object, model: object) -> object:
        self.captured = {"messages": messages, "model": model}
        # mimic the OpenAI-shaped response the hosted path unwraps
        msg = type("Msg", (), {"content": "a cat on a mat"})()
        choice = type("Choice", (), {"message": msg})()
        return type("Resp", (), {"choices": [choice]})()


def test_hosted_vlm_builds_data_url_multimodal_request() -> None:
    # The hosted image-text-to-text path must construct a chat_completion call whose
    # user message carries a TEXT part AND an image_url part whose url is a real
    # ``data:<mime>;base64,<...>`` data-URL — the shape the router actually accepts.
    # No network / no model download: the InferenceClient is stubbed and captures
    # the messages argument.
    stub = _CapturingInferenceClient()
    be = InferenceClientBackend(client=stub)
    image = b"\x89PNG\r\n\x1a\n-pretend-png-bytes"

    out = be.image_text_to_text(image, "What is in this image?", "acme/vlm")
    assert out == "a cat on a mat"

    messages = stub.captured["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    content = messages[0]["content"]
    text_parts = [p for p in content if p["type"] == "text"]
    image_parts = [p for p in content if p["type"] == "image_url"]
    assert text_parts == [{"type": "text", "text": "What is in this image?"}]
    assert len(image_parts) == 1
    url = image_parts[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    # the data-URL payload is the EXACT base64 of the image bytes (round-trips)
    assert url == f"data:image/png;base64,{base64.b64encode(image).decode()}"
    assert stub.captured["model"] == "acme/vlm"
