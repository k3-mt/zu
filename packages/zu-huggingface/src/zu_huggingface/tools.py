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


# --- §6.4 breadth: the wider task surface -------------------------------------

_IMAGE_PARAMS = {
    "type": "object",
    "properties": {
        "data_b64": {"type": "string", "description": "base64-encoded image"},
        "path": {"type": "string", "description": "local image file path"},
    },
}


def _image_question_params(media: str) -> dict:
    return {
        "type": "object",
        "properties": {
            "data_b64": {"type": "string", "description": f"base64-encoded {media}"},
            "path": {"type": "string", "description": f"local {media} file path"},
            "question": {"type": "string"},
        },
        "required": ["question"],
    }


class SegmentImage(_HfTool):
    """Image segmentation — image → labelled masks. Role: Tool (§8.5 CV)."""

    name = "hf_segment"
    prompt_fragment = "hf_segment(data_b64|path): segment an image into labelled regions (masks)."
    schema = {
        "name": "hf_segment",
        "description": "Segment an image into labelled regions via a HuggingFace model.",
        "parameters": _IMAGE_PARAMS,
    }

    async def __call__(self, ctx: Any, data_b64: str | None = None, path: str | None = None) -> dict:
        image = _decode_media(data_b64, path)
        _ = Image(data=image)  # typed currency in
        segments = self._c().image_segmentation(image, self.model)
        return {"segments": segments, "count": len(segments), "model": self.model}


class EstimateDepth(_HfTool):
    """Depth estimation — image → depth map (base64 PNG). Role: Tool (§8.5 CV)."""

    name = "hf_depth"
    prompt_fragment = "hf_depth(data_b64|path): estimate per-pixel depth of an image (PNG depth map)."
    schema = {
        "name": "hf_depth",
        "description": "Estimate the depth map of an image via a HuggingFace model.",
        "parameters": _IMAGE_PARAMS,
    }

    async def __call__(self, ctx: Any, data_b64: str | None = None, path: str | None = None) -> dict:
        image = _decode_media(data_b64, path)
        _ = Image(data=image)
        out = self._c().depth_estimation(image, self.model)
        return {"depth_png_b64": out["depth_png_b64"], "model": self.model}


class AskDocument(_HfTool):
    """Document QA — (document image + question) → answer. Role: Tool (§8.5)."""

    name = "hf_doc_qa"
    prompt_fragment = "hf_doc_qa(data_b64|path, question): answer a question about a document image."
    schema = {
        "name": "hf_doc_qa",
        "description": "Answer a question about a document image via a HuggingFace model.",
        "parameters": _image_question_params("document image"),
    }

    async def __call__(
        self, ctx: Any, question: str, data_b64: str | None = None, path: str | None = None
    ) -> dict:
        image = _decode_media(data_b64, path)
        _ = Image(data=image)
        _ = Text(text=question)
        out = self._c().document_question_answering(image, question, self.model)
        return {**out, "model": self.model}


class AskImage(_HfTool):
    """Visual QA — (image + question) → answer. Role: Tool (§8.5 Multimodal)."""

    name = "hf_vqa"
    prompt_fragment = "hf_vqa(data_b64|path, question): answer a question about an image."
    schema = {
        "name": "hf_vqa",
        "description": "Answer a question about an image via a HuggingFace VQA model.",
        "parameters": _image_question_params("image"),
    }

    async def __call__(
        self, ctx: Any, question: str, data_b64: str | None = None, path: str | None = None
    ) -> dict:
        image = _decode_media(data_b64, path)
        _ = Image(data=image)
        _ = Text(text=question)
        out = self._c().visual_question_answering(image, question, self.model)
        return {**out, "model": self.model}


class Speak(_HfTool):
    """Text-to-speech — text → audio (the inverse of Transcribe). Role: Tool.

    The only tool whose typed Content output is non-text: the synthesised bytes
    are wrapped :class:`Audio` (currency out) then base64-encoded into the dict.
    """

    name = "hf_speak"
    prompt_fragment = "hf_speak(text): synthesise speech audio from text (returns base64 WAV)."
    schema = {
        "name": "hf_speak",
        "description": "Synthesise speech audio from text via a HuggingFace TTS model.",
        "parameters": {
            "type": "object",
            "properties": {"text": {"type": "string"}},
            "required": ["text"],
        },
    }

    async def __call__(self, ctx: Any, text: str) -> dict:
        audio = self._c().text_to_speech(text, self.model)
        clip = Audio(data=audio, mime="audio/wav")  # typed currency OUT
        return {
            "audio_b64": base64.b64encode(clip.data).decode(),
            "mime": clip.mime,
            "model": self.model,
        }


class ClassifyAudio(_HfTool):
    """Audio classification — audio → labels. Role: Tool (or detector/validator).

    Funnels through the same ``[{label,score}]`` shape as the text classifier, so
    it is interchangeable with :class:`HfClassifierDetector`/``Validator``."""

    name = "hf_classify_audio"
    prompt_fragment = "hf_classify_audio(data_b64|path): classify/score an audio clip into labels."
    schema = {
        "name": "hf_classify_audio",
        "description": "Classify an audio clip via a HuggingFace audio-classification model.",
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
        _ = Audio(data=audio)
        labels = self._c().audio_classification(audio, self.model)
        return {"labels": labels, "top": labels[0]["label"] if labels else None, "model": self.model}


class VlmDescribe(_HfTool):
    """VLM-as-tool — (image + text prompt) → text. Role: Tool (§8.5 Multimodal).

    A multimodal model exposed as a *verb*, not the policy: it lets a TEXT policy
    reason over a picture by asking the VLM to describe/answer about it. Typed:
    Image + Text in, Text out. The vision rides the client's image-text-to-text
    path (a multimodal chat call hosted; an image-text-to-text pipeline local).
    """

    name = "hf_vlm"
    prompt_fragment = "hf_vlm(data_b64|path, prompt): ask a vision-language model about an image."
    schema = {
        "name": "hf_vlm",
        "description": "Describe or answer about an image via a HuggingFace vision-language model.",
        "parameters": {
            "type": "object",
            "properties": {
                "data_b64": {"type": "string", "description": "base64-encoded image"},
                "path": {"type": "string", "description": "local image file path"},
                "prompt": {"type": "string"},
            },
            "required": ["prompt"],
        },
    }

    async def __call__(
        self, ctx: Any, prompt: str, data_b64: str | None = None, path: str | None = None
    ) -> dict:
        image = _decode_media(data_b64, path)
        _ = Image(data=image)
        _ = Text(text=prompt)
        out = self._c().image_text_to_text(image, prompt, self.model)
        _ = Text(text=out)  # typed currency OUT
        return {"text": out, "model": self.model}


class AskTable(_HfTool):
    """Table QA — (table + question) → answer. Role: Tool (§8.5). Pure-structured."""

    name = "hf_table_qa"
    prompt_fragment = "hf_table_qa(table, question): answer a question over a table (column→cells)."
    schema = {
        "name": "hf_table_qa",
        "description": "Answer a question over a table via a HuggingFace TableQA model.",
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "object",
                    "description": "column name → list of cell strings",
                    "additionalProperties": {"type": "array", "items": {"type": "string"}},
                },
                "question": {"type": "string"},
            },
            "required": ["table", "question"],
        },
    }

    async def __call__(self, ctx: Any, table: dict[str, list[str]], question: str) -> dict:
        _ = Text(text=question)
        out = self._c().table_question_answering(table, question, self.model)
        return {**out, "model": self.model}


class ClassifyTable(_HfTool):
    """Tabular classification — rows → one label per row. Role: Tool (hosted-only).

    Tabular models are sklearn/tabular-backed on the Hub and served only via the
    Inference API; the local pipeline raises a clear hosted-only error (it never
    fetches a model, so it cannot bypass the supply-chain guard)."""

    name = "hf_tabular_classify"
    prompt_fragment = "hf_tabular_classify(table): predict a class label per row of a table."
    schema = {
        "name": "hf_tabular_classify",
        "description": "Predict a class label per row via a HuggingFace tabular model (hosted).",
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "object",
                    "additionalProperties": {"type": "array", "items": {"type": "string"}},
                }
            },
            "required": ["table"],
        },
    }

    async def __call__(self, ctx: Any, table: dict[str, list[str]]) -> dict:
        labels = self._c().tabular_classification(table, self.model)
        return {"labels": labels, "model": self.model}


class PredictTable(_HfTool):
    """Tabular regression — rows → one number per row. Role: Tool (hosted-only)."""

    name = "hf_tabular_regress"
    prompt_fragment = "hf_tabular_regress(table): predict a numeric value per row of a table."
    schema = {
        "name": "hf_tabular_regress",
        "description": "Predict a numeric value per row via a HuggingFace tabular model (hosted).",
        "parameters": {
            "type": "object",
            "properties": {
                "table": {
                    "type": "object",
                    "additionalProperties": {"type": "array", "items": {"type": "string"}},
                }
            },
            "required": ["table"],
        },
    }

    async def __call__(self, ctx: Any, table: dict[str, list[str]]) -> dict:
        values = self._c().tabular_regression(table, self.model)
        return {"values": values, "model": self.model}
