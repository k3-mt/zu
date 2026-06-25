"""zu-huggingface — HuggingFace models behind Zu's typed ports (§8.3–8.5).

HuggingFace is not a model — it is the largest hub of open models across every
modality. This package reaches it three ways, all behind configuration:

* **Chat / vision-language models as the policy** need *no code here* — they
  speak the OpenAI chat API on all three serving surfaces (the router's ``/v1``,
  an Endpoint's ``/v1``, or a local vLLM), so a HuggingFace model as the brain
  is the existing ``openai-compatible`` provider pointed at a HuggingFace base
  URL (see this package's README). It is the OpenRouter story exactly.

* **Task models** (ASR, OCR, detection, embeddings, classification,
  summarisation, translation) are *not* chat models — each has its own typed
  I/O — so they enter through the non-policy ports by their role: as **Tools**
  (``tools.py``) and as **detectors / validators** (``roles.py``), over the one
  :class:`HfClient` seam (``client.py``) that works hosted or local.

* **The supply chain** (``supply_chain.py``) makes pulling any of them safe by
  default: pin + hash, safetensors not pickle, never trust remote code.
"""

from __future__ import annotations

from .client import HF_ROUTER, HfClient, InferenceClientBackend, PipelineBackend
from .roles import HfClassifierDetector, HfClassifierValidator
from .supply_chain import (
    ModelPin,
    SupplyChainError,
    SupplyChainPolicy,
    assert_no_remote_code,
    file_sha256,
    safe_pipeline_kwargs,
    verify_file_hash,
    verify_model_source,
)
from .tools import (
    AskDocument,
    AskImage,
    AskTable,
    Classify,
    ClassifyAudio,
    ClassifyTable,
    DetectObjects,
    Embed,
    EstimateDepth,
    ImageToText,
    PredictTable,
    SegmentImage,
    Speak,
    Summarize,
    Transcribe,
    Translate,
    VlmDescribe,
    ZeroShotClassify,
)

__all__ = [
    # client seam
    "HfClient",
    "HF_ROUTER",
    "InferenceClientBackend",
    "PipelineBackend",
    # tools
    "Transcribe",
    "ImageToText",
    "DetectObjects",
    "Embed",
    "Classify",
    "ZeroShotClassify",
    "Summarize",
    "Translate",
    # §6.4 breadth
    "SegmentImage",
    "EstimateDepth",
    "AskDocument",
    "AskImage",
    "Speak",
    "ClassifyAudio",
    "VlmDescribe",
    "AskTable",
    "ClassifyTable",
    "PredictTable",
    # role wrappers
    "HfClassifierDetector",
    "HfClassifierValidator",
    # supply chain
    "ModelPin",
    "SupplyChainPolicy",
    "SupplyChainError",
    "verify_model_source",
    "assert_no_remote_code",
    "safe_pipeline_kwargs",
    "file_sha256",
    "verify_file_hash",
]
