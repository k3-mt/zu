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

from .supply_chain import (
    ModelPin,
    SupplyChainPolicy,
    resolve_cached_snapshot,
    safe_pipeline_kwargs,
    verify_file_hash,
)

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


class MalformedModelOutput(ValueError):
    """A task model returned output that the normaliser cannot interpret.

    Raised instead of silently coercing the value to a default label/score or an
    empty answer, so a downstream detector/validator can tell a *real* result
    (legitimately empty) apart from a *coerced-away failure* (unparseable model
    output). Carries the offending ``raw`` value so the surfaced signal is
    diagnosable. The tools catch this and return a structured error observation
    (``{"error": …, "unparseable": …}``); a detector/validator lets it propagate,
    where the loop surfaces it as a counted ``harness.check.crashed`` event —
    either way the failure is visible, never swallowed."""

    def __init__(self, task: str, raw: Any) -> None:
        self.task = task
        self.raw = raw
        super().__init__(
            f"{task}: model output is unparseable — the normaliser cannot read "
            f"a {{label,score}} / answer from {type(raw).__name__} {raw!r:.200}"
        )


def _scores(raw: Any, *, task: str = "classification") -> list[dict]:
    """Normalise a classifier response to ``[{"label","score"}, …]`` sorted by
    score desc — the shape every classification tool/detector reads.

    An *empty* recognised container (``[]`` or a zero-shot dict with no labels) is
    a legitimate "no labels" result and returns ``[]``. But an *unrecognisable*
    shape — a value that is neither a list nor the zero-shot dict, or a non-empty
    list whose every element lacks a ``label`` — is a malformed response, not a
    real empty: it is SURFACED via :class:`MalformedModelOutput` rather than
    silently coerced to ``[]``."""
    if isinstance(raw, dict) and "labels" in raw and "scores" in raw:  # zero-shot shape
        out = [{"label": str(lbl), "score": float(sc)}
               for lbl, sc in zip(raw["labels"], raw["scores"], strict=False)]
    elif isinstance(raw, list):
        out = [
            {"label": str(item["label"]), "score": float(item.get("score", 0.0))}
            for item in raw
            if isinstance(item, dict) and "label" in item
        ]
        # A non-empty list that yielded nothing usable was silently dropped before;
        # that hides a real failure, so surface it instead.
        if raw and not out:
            raise MalformedModelOutput(task, raw)
    else:
        # Neither a list nor a zero-shot dict: unrecognisable, not "no result".
        raise MalformedModelOutput(task, raw)
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


def _to_nested_floats(raw: Any) -> list | None:
    """A raw depth tensor/ndarray (torch/numpy) or nested sequence → a 2-D nested
    list of floats, JSON-safe. Returns None when there is nothing recoverable. The
    visualisation PNG is min/max-normalised (0–255), so it loses absolute distance;
    this preserves the RAW per-pixel magnitudes so a consumer can recover them."""
    if raw is None:
        return None
    arr = raw
    # torch tensor → numpy (detach if it carries grad); numpy stays as-is.
    if hasattr(arr, "detach"):
        arr = arr.detach()
    if hasattr(arr, "cpu"):
        arr = arr.cpu()
    if hasattr(arr, "numpy"):
        try:
            arr = arr.numpy()
        except Exception:  # noqa: BLE001 - fall through to the generic tolist() path
            pass
    if hasattr(arr, "squeeze"):
        try:
            arr = arr.squeeze()
        except Exception:  # noqa: BLE001 - some sequences lack squeeze; ignore
            pass
    if hasattr(arr, "tolist"):
        arr = arr.tolist()
    if not isinstance(arr, list):
        return None
    # Collapse a leading singleton batch/channel dim (e.g. [[ [row…], … ]]).
    while len(arr) == 1 and arr and isinstance(arr[0], list):
        arr = arr[0]
    if not arr or not isinstance(arr[0], list):
        return None
    return [[float(v) for v in row] for row in arr]


def _depth_magnitudes(raw: Any) -> dict:
    """Summarise raw depth magnitudes into a JSON-safe block: ``min``/``max`` and the
    full per-pixel ``depth`` grid (2-D nested floats). Empty ``{}`` when no raw depth
    is available (a backend that only returns the visualisation) — additive, so the
    existing ``depth_png_b64`` shape is never broken."""
    grid = _to_nested_floats(raw)
    if grid is None:
        return {}
    flat = [v for row in grid for v in row]
    if not flat:
        return {}
    return {"depth": grid, "depth_min": min(flat), "depth_max": max(flat)}


def _depth_to_b64(depth: Any, raw: Any = None) -> dict:
    """A depth-estimation result: ``{"depth_png_b64": <str>}`` (the normalised
    visualisation) PLUS, when the backend exposes them, the RAW per-pixel depth
    magnitudes (``depth``/``depth_min``/``depth_max``) so a consumer needing real
    distances can recover them — the PNG alone is min/max-normalised and lossy."""
    return {"depth_png_b64": _pil_to_png_b64(depth), **_depth_magnitudes(raw)}


def _qa_top(raw: Any, *, task: str = "qa") -> dict:
    """The top element of a (document/visual) QA response as ``{answer, score}``.

    A genuinely empty response (``[]`` / ``None``) is a legitimate "no answer" and
    returns ``{"answer": ""}``. But an element that is neither a mapping nor
    carries an ``answer`` attribute is unparseable model output — previously it was
    silently coerced to an empty answer (indistinguishable from a real one), so it
    is now SURFACED via :class:`MalformedModelOutput`."""
    el = raw[0] if isinstance(raw, list) and raw else raw
    if el is None or (isinstance(raw, list) and not raw):
        return {"answer": ""}
    has_answer = isinstance(el, dict) and "answer" in el or hasattr(el, "answer")
    if not has_answer:
        raise MalformedModelOutput(task, raw)
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
        raw = (r.get("predicted_depth") if isinstance(r, dict)
               else getattr(r, "predicted_depth", None))
        return _depth_to_b64(depth, raw)

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

    The only option for air-gapped / on-prem, and it **reaches no network**: the
    cache is populated out of band, and every pipeline is built offline. Each one
    is built through the §8.3 supply-chain guards, in this order:

    1. resolve the model's snapshot **from the local cache** via
       :func:`resolve_cached_snapshot` (``snapshot_download(..., local_files_only=
       True)`` — no egress; a cache miss raises, the correct fail-closed
       behaviour);
    2. run the pickle / ``.bin/.pt/.ckpt`` rejection over that **real** cached
       file set, and hash-verify every ``pin.expected_hashes`` entry against the
       file on disk — all *before* ``transformers.pipeline`` is constructed;
    3. build with :func:`safe_pipeline_kwargs` (``trust_remote_code=False``,
       ``local_files_only=True``, ``use_safetensors=True``, pinned revision).

    The process env carries ``HF_HUB_OFFLINE`` / ``TRANSFORMERS_OFFLINE`` too,
    because transformers honours ``local_files_only`` inconsistently across tasks.
    A :class:`ModelPin` (with a revision and optional ``expected_hashes``) may be
    supplied per model id; otherwise one is synthesised from the model string and
    the verification still runs against the cache. Pipelines are cached per
    (task, model).
    """

    egress_host = ""  # local — no egress

    def __init__(
        self,
        policy: SupplyChainPolicy | None = None,
        *,
        pins: dict[str, ModelPin] | None = None,
    ) -> None:
        self._policy = policy or SupplyChainPolicy()
        self._cache: dict[tuple[str, str], Any] = {}
        # Optional real pins (revision + expected_hashes) keyed by repo id; a model
        # without one falls back to a synthesised ModelPin(repo_id=model).
        self._pins: dict[str, ModelPin] = dict(pins or {})

    def _pin_for(self, model: str) -> ModelPin:
        """The real :class:`ModelPin` for ``model`` (revision + expected_hashes)
        when one was supplied, else a bare pin synthesised from the model id."""
        return self._pins.get(model, ModelPin(repo_id=model))

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
            # Belt-and-suspenders zero-egress: transformers applies local_files_only
            # inconsistently, so force the offline env flags too before any load.
            os.environ["HF_HUB_OFFLINE"] = "1"
            os.environ["TRANSFORMERS_OFFLINE"] = "1"

            pin = self._pin_for(model)
            # Resolve the file set FROM THE LOCAL CACHE (no network); a cache miss
            # raises here — fail closed. This is also where require_pinned_revision
            # is enforced before any cache lookup.
            safe_pipeline_kwargs(pin, self._policy)  # cheap pin checks, may raise
            snapshot, files = resolve_cached_snapshot(pin)
            # The pickle/.bin/.pt/.ckpt rejection + per-file hash check run against
            # the REAL cached files, before the pipeline is constructed.
            kwargs = safe_pipeline_kwargs(pin, self._policy, files=files)
            for filename, expected in pin.expected_hashes.items():
                verify_file_hash(snapshot / filename, expected)

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
        depth = r["depth"] if isinstance(r, dict) else getattr(r, "depth", None)
        raw = (r.get("predicted_depth") if isinstance(r, dict)
               else getattr(r, "predicted_depth", None))
        return _depth_to_b64(depth, raw)

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
