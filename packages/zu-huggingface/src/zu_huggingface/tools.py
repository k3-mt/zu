"""HuggingFace task models as typed Zu Tools (Engineering Design §8.5).

Each tool wraps one HuggingFace task behind the standard Tool contract, with the
typed multimodal :class:`~zu_core.content` Content (Text/Image/Audio) as the
currency in and out — which is what lets a non-chat model slot into the loop as
cleanly as a chat one. The same tool works hosted or local because it depends
only on the :class:`HfClient` seam (``client.py``).

The port is the *role*, assigned per agent (§4.5): these are Tools — verbs the
policy performs (transcribe, read an image, detect, embed, summarise,
translate). A classifier wanting to *gate control flow* or *check a result*
becomes a detector/validator instead — see ``roles.py``.

The envelope is derived from the backend: a hosted client egresses to the HF
router (CAP_NET + that host); a local pipeline reaches nothing. Media is passed
as base64 (``data_b64``) or a local ``path`` — the realistic shape when the
policy carries bytes from a prior observation.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from zu_core.content import Audio, Image, Text
from zu_core.ports import CAP_NET

from .client import HfClient, InferenceClientBackend


def _decode_media(data_b64: str | None, path: str | None) -> bytes:
    if data_b64:
        return base64.b64decode(data_b64)
    if path:
        return Path(path).read_bytes()
    raise ValueError("provide media as 'data_b64' (base64) or a local 'path'")


class _HfTool:
    """Shared base: hold a model id + client, and derive the capability envelope
    from the backend (hosted ⇒ net+router; local ⇒ nothing)."""

    tier = 1  # a specialised model the policy calls — cheap, not an escalation

    def __init__(self, model: str, client: HfClient | None = None) -> None:
        self.model = model
        self._client = client
        backend = client if client is not None else InferenceClientBackend()
        host = getattr(backend, "egress_host", "")
        self.capabilities = frozenset({CAP_NET}) if host else frozenset()
        self.egress = frozenset({host}) if host else frozenset()
        self._backend = backend

    def _c(self) -> HfClient:
        return self._client if self._client is not None else self._backend


class Transcribe(_HfTool):
    """ASR — audio → text (a sense). Role: Tool (§8.5 Audio)."""

    name = "hf_transcribe"
    prompt_fragment = "hf_transcribe(data_b64|path): transcribe speech audio to text."
    schema = {
        "name": "hf_transcribe",
        "description": "Transcribe speech audio to text via a HuggingFace ASR model.",
        "parameters": {
            "type": "object",
            "properties": {
                "data_b64": {"type": "string", "description": "base64-encoded audio"},
                "path": {"type": "string", "description": "local audio file path"},
            },
        },
    }

    async def __call__(self, ctx: Any, data_b64: str | None = None, path: str | None = None) -> dict:
        audio = _decode_media(data_b64, path)
        _ = Audio(data=audio)  # typed currency in (recorded shape)
        text = self._c().transcribe(audio, self.model)
        return {"text": text, "model": self.model}


class ImageToText(_HfTool):
    """Image-to-text / OCR — image → text (a sense). Role: Tool (§8.5 CV/Multimodal)."""

    name = "hf_image_to_text"
    prompt_fragment = "hf_image_to_text(data_b64|path): read/describe an image as text (OCR or caption)."
    schema = {
        "name": "hf_image_to_text",
        "description": "Extract or describe the text/content of an image via a HuggingFace model.",
        "parameters": {
            "type": "object",
            "properties": {
                "data_b64": {"type": "string", "description": "base64-encoded image"},
                "path": {"type": "string", "description": "local image file path"},
            },
        },
    }

    async def __call__(self, ctx: Any, data_b64: str | None = None, path: str | None = None) -> dict:
        image = _decode_media(data_b64, path)
        _ = Image(data=image)
        text = self._c().image_to_text(image, self.model)
        return {"text": text, "model": self.model}


class DetectObjects(_HfTool):
    """Object detection — image → boxes. Role: Tool (or detector). (§8.5 CV)."""

    name = "hf_detect"
    prompt_fragment = "hf_detect(data_b64|path): find objects in an image (labelled boxes)."
    schema = {
        "name": "hf_detect",
        "description": "Detect objects in an image via a HuggingFace model; returns labelled boxes.",
        "parameters": {
            "type": "object",
            "properties": {
                "data_b64": {"type": "string", "description": "base64-encoded image"},
                "path": {"type": "string", "description": "local image file path"},
            },
        },
    }

    async def __call__(self, ctx: Any, data_b64: str | None = None, path: str | None = None) -> dict:
        image = _decode_media(data_b64, path)
        objects = self._c().object_detection(image, self.model)
        return {"objects": objects, "count": len(objects), "model": self.model}


class Embed(_HfTool):
    """Feature extraction — text → vector. Role: retrieval Tool / grounding (§8.5 NLP)."""

    name = "hf_embed"
    prompt_fragment = "hf_embed(text): embed text into a vector for search/similarity."
    schema = {
        "name": "hf_embed",
        "description": "Embed text into a dense vector via a HuggingFace embedding model.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }

    async def __call__(self, ctx: Any, text: str) -> dict:
        vec = self._c().embed(text, self.model)
        return {"embedding": vec, "dim": len(vec), "model": self.model}


class Classify(_HfTool):
    """Text classification — text → labels. Role: Tool (or detector/router) (§8.5 NLP)."""

    name = "hf_classify"
    prompt_fragment = "hf_classify(text): classify/score text into the model's labels."
    schema = {
        "name": "hf_classify",
        "description": "Classify text via a HuggingFace text-classification model.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }

    async def __call__(self, ctx: Any, text: str) -> dict:
        labels = self._c().text_classification(text, self.model)
        return {"labels": labels, "top": labels[0]["label"] if labels else None, "model": self.model}


class ZeroShotClassify(_HfTool):
    """Zero-shot classification — text + candidate labels → scores (§8.5 NLP)."""

    name = "hf_zero_shot"
    prompt_fragment = "hf_zero_shot(text, labels): score text against candidate labels you supply."
    schema = {
        "name": "hf_zero_shot",
        "description": "Zero-shot classify text against candidate labels via a HuggingFace model.",
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string"},
                "labels": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["text", "labels"],
        },
    }

    async def __call__(self, ctx: Any, text: str, labels: list[str]) -> dict:
        scored = self._c().zero_shot(text, labels, self.model)
        return {"labels": scored, "top": scored[0]["label"] if scored else None, "model": self.model}


class Summarize(_HfTool):
    """Summarization — text → text (§8.5 NLP)."""

    name = "hf_summarize"
    prompt_fragment = "hf_summarize(text): summarise a long text."
    schema = {
        "name": "hf_summarize",
        "description": "Summarise text via a HuggingFace summarization model.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }

    async def __call__(self, ctx: Any, text: str) -> dict:
        out = self._c().summarize(text, self.model)
        _ = Text(text=out)
        return {"text": out, "model": self.model}


class Translate(_HfTool):
    """Translation — text → text (§8.5 NLP)."""

    name = "hf_translate"
    prompt_fragment = "hf_translate(text): translate text (model is pinned to a language pair)."
    schema = {
        "name": "hf_translate",
        "description": "Translate text via a HuggingFace translation model.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }

    async def __call__(self, ctx: Any, text: str) -> dict:
        out = self._c().translate(text, self.model)
        return {"text": out, "model": self.model}
