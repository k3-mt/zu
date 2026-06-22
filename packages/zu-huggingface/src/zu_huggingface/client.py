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

import os
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
