"""The HuggingFace client seam — one task-method interface, three serving surfaces.

Most HuggingFace models are *not* chat models: OCR, speech recognition, object
detection, and embedding models each have their own typed input/output, so they
enter Zu through the non-policy ports by their role (§8.5). This module is the
thin seam the HuggingFace *tools* call, so the same tool works whether the model
is served hosted (the Inference Providers router) or local (a transformers
pipeline) — the integration is done once, here.

``HfClient`` is the protocol the tools depend on. Two adapters implement it:

* :class:`InferenceClientBackend` — wraps ``huggingface_hub.InferenceClient``
  (hosted; the router or a dedicated Endpoint), egressing to the HF router.
* :class:`PipelineBackend` — wraps ``transformers.pipeline`` (local; the
  air-gapped / on-prem case), constructed only through the supply-chain guards
  (§8.3): a pinned revision and ``trust_remote_code=False``.

Both heavy SDKs are imported lazily, so installing ``zu-huggingface`` without
the extras costs nothing and the tools are testable offline against a fake
client. Credentials (``HF_TOKEN``) are resolved from the environment *inside*
the backend, never placed in the model's context.
"""

from __future__ import annotations

import base64
import io
import os
import struct
from typing import Any, Protocol, runtime_checkable

from .supply_chain import ModelPin, SupplyChainPolicy, safe_pipeline_kwargs

# The Inference Providers router — the hosted default, OpenAI-compatible for
# chat at /v1 but task-native through the InferenceClient methods.
HF_ROUTER = "router.huggingface.co"


@runtime_checkable
class HfClient(Protocol):
    """The task methods the HuggingFace tools call. Inputs/outputs are plain
    Python (bytes for media, str for text, list[dict] for structured) so the
    tools own the translation to/from typed :class:`zu_core.content` Content."""

    def transcribe(self, audio: bytes, model: str) -> str: ...
    def image_to_text(self, image: bytes, model: str) -> str: ...
    def object_detection(self, image: bytes, model: str) -> list[dict]: ...
    def text_classification(self, text: str, model: str) -> list[dict]: ...
    def zero_shot(self, text: str, labels: list[str], model: str) -> list[dict]: ...
    def embed(self, text: str, model: str) -> list[float]: ...
    def summarize(self, text: str, model: str) -> str: ...
    def translate(self, text: str, model: str) -> str: ...
    # --- §6.4 breadth: the wider task surface, one contract on both backends ---
    def image_segmentation(self, image: bytes, model: str) -> list[dict]: ...
    def depth_estimation(self, image: bytes, model: str) -> dict: ...
    def document_question_answering(self, image: bytes, question: str, model: str) -> dict: ...
    def visual_question_answering(self, image: bytes, question: str, model: str) -> dict: ...
    def text_to_speech(self, text: str, model: str) -> bytes: ...
    def audio_classification(self, audio: bytes, model: str) -> list[dict]: ...
    def image_text_to_text(self, image: bytes, prompt: str, model: str) -> str: ...
    def table_question_answering(
        self, table: dict[str, list[str]], question: str, model: str
    ) -> dict: ...
    def tabular_classification(self, table: dict[str, list[str]], model: str) -> list[str]: ...
    def tabular_regression(self, table: dict[str, list[str]], model: str) -> list[float]: ...


def _scores(raw: Any) -> list[dict]:
    """Normalise a classifier response to ``[{"label","score"}, …]`` sorted by
    score desc — the shape every classification tool/detector reads."""
    out: list[dict] = []
    if isinstance(raw, dict) and "labels" in raw and "scores" in raw:  # zero-shot shape
        out = [{"label": str(lbl), "score": float(sc)}
               for lbl, sc in zip(raw["labels"], raw["scores"], strict=False)]
    elif isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and "label" in item:
                out.append({"label": str(item["label"]), "score": float(item.get("score", 0.0))})
    return sorted(out, key=lambda d: d["score"], reverse=True)


def _pil_to_png_b64(img: Any) -> str:
    """Encode a PIL image (or already-bytes) to base64 PNG. PIL is lazy-imported
    so the hosted/local extras carry the dependency; the offline tests never
    reach this path (the fake client returns pre-encoded strings)."""
    if isinstance(img, (bytes, bytearray)):
        return base64.b64encode(bytes(img)).decode()
    if isinstance(img, str):  # already base64
        return img
    try:
        from PIL import Image as _PILImage  # noqa: F401
    except ImportError as e:  # pragma: no cover - only on a real hosted/local call
        raise RuntimeError(
            "encoding a mask/depth image needs `pillow` "
            "(install zu-huggingface[hosted] or [local])"
        ) from e
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _segments(raw: Any) -> list[dict]:
    """Normalise an image-segmentation response to
    ``[{"label","score","mask_b64"}, …]`` — masks PNG-encoded into base64 so the
    observation stays JSON-safe (never raw bytes), mirroring how Image serialises."""
    out: list[dict] = []
    for el in raw or []:
        label = el.get("label") if isinstance(el, dict) else getattr(el, "label", None)
        score = el.get("score") if isinstance(el, dict) else getattr(el, "score", None)
        mask = el.get("mask") if isinstance(el, dict) else getattr(el, "mask", None)
        seg: dict[str, Any] = {"label": str(label)}
        if score is not None:
            seg["score"] = float(score)
        seg["mask_b64"] = _pil_to_png_b64(mask) if mask is not None else ""
        out.append(seg)
    return out


def _depth_to_b64(depth: Any) -> dict:
    """A depth-estimation result as ``{"depth_png_b64": <str>}`` — the depth map
    serialised as a base64 PNG so the observation is compact and JSON-safe."""
    return {"depth_png_b64": _pil_to_png_b64(depth)}


def _qa_top(raw: Any) -> dict:
    """The top element of a (document/visual) QA response as ``{answer, score}``."""
    el = raw[0] if isinstance(raw, list) and raw else raw
    if el is None:
        return {"answer": ""}
    answer = el.get("answer") if isinstance(el, dict) else getattr(el, "answer", None)
    score = el.get("score") if isinstance(el, dict) else getattr(el, "score", None)
    out: dict[str, Any] = {"answer": str(answer) if answer is not None else ""}
    if score is not None:
        out["score"] = float(score)
    return out


def _wav_bytes(samples: Any, sampling_rate: int) -> bytes:
    """Encode a 1-D float/int sample sequence to a 16-bit PCM mono WAV via the
    stdlib ``wave`` writer (no extra dependency). Used only by the *local* TTS
    path; the hosted path already returns audio bytes. Offline tests never reach
    this (the fake client returns bytes directly) — it has its own unit test fed a
    tiny synthetic sample list."""
    import wave

    seq = samples.tolist() if hasattr(samples, "tolist") else list(samples)
    flat: list[float] = []
    for s in seq:
        flat.extend(s if isinstance(s, (list, tuple)) else [s])
    pcm = bytearray()
    for x in flat:
        v = int(x * 32767) if isinstance(x, float) else int(x)
        v = max(-32768, min(32767, v))
        pcm += struct.pack("<h", v)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(int(sampling_rate))
        w.writeframes(bytes(pcm))
    return buf.getvalue()


def _data_url(image: bytes, mime: str = "image/png") -> str:
    """A ``data:`` URL for an image — the multimodal envelope the router's chat
    surface accepts for the image-text-to-text (VLM-as-tool) path."""
    return f"data:{mime};base64,{base64.b64encode(image).decode()}"


_TABULAR_LOCAL_ERROR = (
    "tabular tasks are hosted-only — transformers has no first-class local "
    "pipeline for tabular-classification/regression (they are sklearn/tabular "
    "models served via the Inference API). Use the hosted InferenceClientBackend."
)


class InferenceClientBackend:
    """Hosted HuggingFace via ``huggingface_hub.InferenceClient`` (lazy import).

    The same model id works through the serverless router or a dedicated
    Endpoint; ``HF_TOKEN`` is read from the environment here.
    """

    egress_host = HF_ROUTER

    def __init__(
        self,
        *,
        provider: str = "hf-inference",
        token_env: str = "HF_TOKEN",
        client: Any = None,
    ) -> None:
        self._provider = provider
        self._token_env = token_env
        self._client = client  # injectable for tests

    def _c(self) -> Any:
        if self._client is None:
            try:
                from huggingface_hub import InferenceClient
            except ImportError as e:  # pragma: no cover - exercised only without the extra
                raise RuntimeError(
                    "the hosted HuggingFace backend needs `huggingface_hub` "
                    "(install zu-huggingface[hosted])"
                ) from e
            self._client = InferenceClient(provider=self._provider, api_key=os.environ.get(self._token_env))
        return self._client

    def transcribe(self, audio: bytes, model: str) -> str:
        r = self._c().automatic_speech_recognition(audio, model=model)
        return r if isinstance(r, str) else str(getattr(r, "text", r))

    def image_to_text(self, image: bytes, model: str) -> str:
        r = self._c().image_to_text(image, model=model)
        return r if isinstance(r, str) else str(getattr(r, "generated_text", r))

    def object_detection(self, image: bytes, model: str) -> list[dict]:
        r = self._c().object_detection(image, model=model)
        return [dict(item) for item in r]

    def text_classification(self, text: str, model: str) -> list[dict]:
        return _scores(self._c().text_classification(text, model=model))

    def zero_shot(self, text: str, labels: list[str], model: str) -> list[dict]:
        return _scores(self._c().zero_shot_classification(text, candidate_labels=labels, model=model))

    def embed(self, text: str, model: str) -> list[float]:
        r = self._c().feature_extraction(text, model=model)
        return [float(x) for x in (r.tolist() if hasattr(r, "tolist") else r)]

    def summarize(self, text: str, model: str) -> str:
        r = self._c().summarization(text, model=model)
        return r if isinstance(r, str) else str(getattr(r, "summary_text", r))

    def translate(self, text: str, model: str) -> str:
        r = self._c().translation(text, model=model)
        return r if isinstance(r, str) else str(getattr(r, "translation_text", r))

    def image_segmentation(self, image: bytes, model: str) -> list[dict]:
        return _segments(self._c().image_segmentation(image, model=model))

    def depth_estimation(self, image: bytes, model: str) -> dict:
        r = self._c().depth_estimation(image, model=model)
        depth = getattr(r, "depth", None) if not isinstance(r, dict) else r.get("depth")
        return _depth_to_b64(depth)

    def document_question_answering(self, image: bytes, question: str, model: str) -> dict:
        return _qa_top(
            self._c().document_question_answering(image, question=question, model=model)
        )

    def visual_question_answering(self, image: bytes, question: str, model: str) -> dict:
        return _qa_top(
            self._c().visual_question_answering(image, question=question, model=model)
        )

    def text_to_speech(self, text: str, model: str) -> bytes:
        r = self._c().text_to_speech(text, model=model)
        return bytes(r)

    def audio_classification(self, audio: bytes, model: str) -> list[dict]:
        return _scores(self._c().audio_classification(audio, model=model))

    def image_text_to_text(self, image: bytes, prompt: str, model: str) -> str:
        # The VLM-as-tool path: a multimodal chat call (text part + image_url
        # data-URL) — what the router's chat surface accepts for image-text-to-text.
        r = self._c().chat_completion(
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": _data_url(image)}},
                    ],
                }
            ],
            model=model,
        )
        return str(r.choices[0].message.content)

    def table_question_answering(
        self, table: dict[str, list[str]], question: str, model: str
    ) -> dict:
        r = self._c().table_question_answering(table=table, query=question, model=model)
        answer = r.get("answer") if isinstance(r, dict) else getattr(r, "answer", None)
        cells = r.get("cells") if isinstance(r, dict) else getattr(r, "cells", None)
        aggregator = (
            r.get("aggregator") if isinstance(r, dict) else getattr(r, "aggregator", None)
        )
        out: dict[str, Any] = {"answer": str(answer) if answer is not None else ""}
        if cells is not None:
            out["cells"] = [str(c) for c in cells]
        if aggregator is not None:
            out["aggregator"] = str(aggregator)
        return out

    def tabular_classification(self, table: dict[str, list[str]], model: str) -> list[str]:
        return [str(x) for x in self._c().tabular_classification(table=table, model=model)]

    def tabular_regression(self, table: dict[str, list[str]], model: str) -> list[float]:
        return [float(x) for x in self._c().tabular_regression(table=table, model=model)]


class PipelineBackend:
    """Local HuggingFace via ``transformers.pipeline`` (lazy import).

    The only option for air-gapped / on-prem. Every pipeline is built through
    :func:`safe_pipeline_kwargs` — a pinned revision and ``trust_remote_code``
    forced off — so the §8.3 supply-chain rules hold by construction. Pipelines
    are cached per (task, model).
    """

    egress_host = ""  # local — no egress

    def __init__(self, policy: SupplyChainPolicy | None = None) -> None:
        self._policy = policy or SupplyChainPolicy()
        self._cache: dict[tuple[str, str], Any] = {}

    def _pipe(self, task: str, model: str) -> Any:
        key = (task, model)
        if key not in self._cache:
            try:
                from transformers import pipeline
            except ImportError as e:  # pragma: no cover - exercised only without the extra
                raise RuntimeError(
                    "the local HuggingFace backend needs `transformers` "
                    "(install zu-huggingface[local])"
                ) from e
            kwargs = safe_pipeline_kwargs(ModelPin(repo_id=model), self._policy)
            self._cache[key] = pipeline(task, **kwargs)
        return self._cache[key]

    def transcribe(self, audio: bytes, model: str) -> str:
        return str(self._pipe("automatic-speech-recognition", model)(audio)["text"])

    def image_to_text(self, image: bytes, model: str) -> str:
        r = self._pipe("image-to-text", model)(image)
        return str(r[0]["generated_text"] if isinstance(r, list) else r["generated_text"])

    def object_detection(self, image: bytes, model: str) -> list[dict]:
        return [dict(item) for item in self._pipe("object-detection", model)(image)]

    def text_classification(self, text: str, model: str) -> list[dict]:
        return _scores(self._pipe("text-classification", model)(text))

    def zero_shot(self, text: str, labels: list[str], model: str) -> list[dict]:
        return _scores(self._pipe("zero-shot-classification", model)(text, candidate_labels=labels))

    def embed(self, text: str, model: str) -> list[float]:
        r = self._pipe("feature-extraction", model)(text)
        # pipelines return [[token-vectors]]; mean-pool to one vector
        vecs = r[0] if isinstance(r, list) else r
        if vecs and isinstance(vecs[0], list):
            cols = list(zip(*vecs, strict=False))
            return [sum(c) / len(c) for c in cols]
        return [float(x) for x in vecs]

    def summarize(self, text: str, model: str) -> str:
        return str(self._pipe("summarization", model)(text)[0]["summary_text"])

    def translate(self, text: str, model: str) -> str:
        return str(self._pipe("translation", model)(text)[0]["translation_text"])

    def image_segmentation(self, image: bytes, model: str) -> list[dict]:
        return _segments(self._pipe("image-segmentation", model)(image))

    def depth_estimation(self, image: bytes, model: str) -> dict:
        r = self._pipe("depth-estimation", model)(image)
        return _depth_to_b64(r["depth"] if isinstance(r, dict) else getattr(r, "depth", None))

    def document_question_answering(self, image: bytes, question: str, model: str) -> dict:
        return _qa_top(
            self._pipe("document-question-answering", model)(image=image, question=question)
        )

    def visual_question_answering(self, image: bytes, question: str, model: str) -> dict:
        return _qa_top(
            self._pipe("visual-question-answering", model)(image=image, question=question)
        )

    def text_to_speech(self, text: str, model: str) -> bytes:
        r = self._pipe("text-to-speech", model)(text)
        return _wav_bytes(r["audio"], int(r.get("sampling_rate", 16000)))

    def audio_classification(self, audio: bytes, model: str) -> list[dict]:
        return _scores(self._pipe("audio-classification", model)(audio))

    def image_text_to_text(self, image: bytes, prompt: str, model: str) -> str:
        r = self._pipe("image-text-to-text", model)(image, prompt)
        if isinstance(r, list) and r:
            r = r[0]
        return str(r["generated_text"] if isinstance(r, dict) else getattr(r, "generated_text", r))

    def table_question_answering(
        self, table: dict[str, list[str]], question: str, model: str
    ) -> dict:
        r = self._pipe("table-question-answering", model)(table=table, query=question)
        return {
            "answer": str(r.get("answer", "")),
            "cells": [str(c) for c in r.get("cells", [])],
            "aggregator": str(r.get("aggregator", "")),
        }

    def tabular_classification(self, table: dict[str, list[str]], model: str) -> list[str]:
        # No model is fetched here — the local branch refuses rather than fall
        # through to a pipeline that would bypass the supply-chain guard.
        raise RuntimeError(_TABULAR_LOCAL_ERROR)

    def tabular_regression(self, table: dict[str, list[str]], model: str) -> list[float]:
        raise RuntimeError(_TABULAR_LOCAL_ERROR)
