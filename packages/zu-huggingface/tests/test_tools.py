"""HuggingFace task models as Zu Tools (§8.5) — offline, against a fake client.

Proves each tool produces the loop-friendly observation shape, calls the right
client method with the right model, and derives its capability envelope from the
backend (hosted ⇒ net + router; local ⇒ nothing).
"""

from __future__ import annotations

import base64
from typing import cast

import pytest

from zu_core.ports import CAP_NET, RunContext
from zu_huggingface import (
    AskDocument,
    AskImage,
    AskTable,
    Classify,
    ClassifyAudio,
    ClassifyTable,
    DetectObjects,
    Embed,
    EstimateDepth,
    HfClassifierDetector,
    HfClient,
    ImageToText,
    InferenceClientBackend,
    MalformedModelOutput,
    ModelPin,
    PipelineBackend,
    PredictTable,
    SegmentImage,
    Speak,
    Summarize,
    SupplyChainPolicy,
    Transcribe,
    Translate,
    VlmDescribe,
    ZeroShotClassify,
)
from zu_huggingface.client import _qa_top, _scores

_B64 = base64.b64encode(b"\x00\x01\x02media").decode()


async def test_transcribe(fake_client) -> None:
    tool = Transcribe("openai/whisper-large-v3", client=fake_client)
    out = await tool(None, data_b64=_B64)
    assert out["text"] == "hello world"
    assert out["model"] == "openai/whisper-large-v3"
    assert fake_client.calls[0][0] == "transcribe"


async def test_image_to_text(fake_client) -> None:
    out = await ImageToText("microsoft/trocr-base", client=fake_client)(None, data_b64=_B64)
    assert "invoice total" in out["text"]


async def test_detect_objects(fake_client) -> None:
    out = await DetectObjects("facebook/detr-resnet-50", client=fake_client)(None, data_b64=_B64)
    assert out["count"] == 1
    assert out["objects"][0]["label"] == "cat"


async def test_embed(fake_client) -> None:
    out = await Embed("BAAI/bge-large", client=fake_client)(None, text="search this")
    assert out["embedding"] == [0.1, 0.2, 0.3]
    assert out["dim"] == 3


async def test_classify(fake_client) -> None:
    out = await Classify("distilbert/sst2", client=fake_client)(None, text="great!")
    assert out["top"] == "POSITIVE"
    assert out["labels"][0]["score"] == 0.97


async def test_zero_shot(fake_client) -> None:
    out = await ZeroShotClassify("facebook/bart-large-mnli", client=fake_client)(
        None, text="ship it", labels=["safe", "unsafe"]
    )
    assert out["top"] == "safe"


async def test_summarize_and_translate(fake_client) -> None:
    assert (await Summarize("facebook/bart-large-cnn", client=fake_client)(None, text="long…"))["text"] == "short summary"
    assert (await Translate("Helsinki-NLP/opus-mt-en-fr", client=fake_client)(None, text="hi"))["text"] == "bonjour le monde"


def test_envelope_hosted_declares_net_and_router(fake_client) -> None:
    hosted = Transcribe("m", client=fake_client)  # default fake egress is the router
    assert hosted.capabilities == frozenset({CAP_NET})
    assert hosted.egress == frozenset({"router.huggingface.co"})


def test_envelope_local_declares_nothing(fake_client) -> None:
    local = Embed("m", client=fake_client.__class__(egress_host=""))
    assert local.capabilities == frozenset()
    assert local.egress == frozenset()


# --- §6.4 breadth: the wider task surface -------------------------------------


async def test_segment_image(fake_client) -> None:
    out = await SegmentImage("nvidia/segformer", client=fake_client)(None, data_b64=_B64)
    assert out["count"] == 2
    assert out["segments"][0]["label"] == "cat"
    assert out["segments"][0]["mask_b64"] == "bWFzazE="  # masks are base64, never raw bytes
    assert out["model"] == "nvidia/segformer"
    assert fake_client.calls[0][0] == "image_segmentation"


async def test_estimate_depth(fake_client) -> None:
    out = await EstimateDepth("Intel/dpt-large", client=fake_client)(None, data_b64=_B64)
    assert out["depth_png_b64"] == "ZGVwdGg="  # depth map is a base64 PNG string
    assert "predicted_depth" not in out  # the raw tensor name is never leaked
    # Raw per-pixel magnitudes are surfaced so a consumer can recover real distances
    # (the PNG alone is min/max-normalised and lossy).
    assert out["depth"] == [[1.0, 2.0], [3.0, 4.0]]
    assert out["depth_min"] == 1.0
    assert out["depth_max"] == 4.0
    assert fake_client.calls[0][0] == "depth_estimation"


async def test_ask_document(fake_client) -> None:
    out = await AskDocument("impira/layoutlm", client=fake_client)(
        None, question="total?", data_b64=_B64
    )
    assert out["answer"] == "42.00"
    assert out["score"] == 0.91
    assert fake_client.calls[0] == ("document_question_answering", b"\x00\x01\x02media", "total?", "impira/layoutlm")


async def test_ask_image(fake_client) -> None:
    out = await AskImage("dandelin/vilt-b32", client=fake_client)(
        None, question="what is it?", data_b64=_B64
    )
    assert out["answer"] == "a cat"
    assert fake_client.calls[0][0] == "visual_question_answering"


async def test_speak_returns_audio_content(fake_client) -> None:
    out = await Speak("microsoft/speecht5_tts", client=fake_client)(None, text="hello")
    assert out["mime"] == "audio/wav"
    assert base64.b64decode(out["audio_b64"]) == b"RIFF....WAVEfmt "  # Audio bytes round-trip
    assert fake_client.calls[0][0] == "text_to_speech"


async def test_classify_audio_matches_classifier_shape(fake_client) -> None:
    out = await ClassifyAudio("MIT/ast", client=fake_client)(None, data_b64=_B64)
    assert out["top"] == "speech"
    assert out["labels"][0]["score"] == 0.95  # same [{label,score}] shape as Classify


async def test_vlm_describe(fake_client) -> None:
    out = await VlmDescribe("Qwen/Qwen2-VL", client=fake_client)(
        None, prompt="describe", data_b64=_B64
    )
    assert "cat" in out["text"]  # Image + Text in -> Text out
    assert out["model"] == "Qwen/Qwen2-VL"
    assert fake_client.calls[0] == ("image_text_to_text", b"\x00\x01\x02media", "describe", "Qwen/Qwen2-VL")


async def test_ask_table(fake_client) -> None:
    table = {"city": ["Paris", "Rome"], "pop": ["2", "3"]}
    out = await AskTable("google/tapas-base", client=fake_client)(
        None, table=table, question="sum?"
    )
    assert out["answer"] == "120"
    assert out["aggregator"] == "SUM"
    assert fake_client.calls[0] == ("table_question_answering", table, "sum?", "google/tapas-base")


async def test_classify_table(fake_client) -> None:
    out = await ClassifyTable("acme/tab-clf", client=fake_client)(None, table={"x": ["1", "2"]})
    assert out["labels"] == ["yes", "no"]  # one label per row


async def test_predict_table(fake_client) -> None:
    out = await PredictTable("acme/tab-reg", client=fake_client)(None, table={"x": ["1", "2"]})
    assert out["values"] == [3.14, 2.72]  # one number per row


async def test_new_tool_envelope_hosted_and_local(fake_client) -> None:
    # one new hosted and one new local tool prove the envelope is *derived* from
    # the backend, never hard-declared (least privilege).
    hosted = SegmentImage("m", client=fake_client)
    assert hosted.capabilities == frozenset({CAP_NET})
    assert hosted.egress == frozenset({"router.huggingface.co"})
    local = Speak("m", client=fake_client.__class__(egress_host=""))
    assert local.capabilities == frozenset()
    assert local.egress == frozenset()


# --- F25: classifier/QA tools SURFACE malformed model output -------------------


class _MalformedClient:
    """A fake HfClient whose classifier/QA methods route through the REAL
    normalisers with an unparseable raw payload — so the tools exercise the
    surface-not-coerce path exactly as a live backend would."""

    egress_host = "router.huggingface.co"

    def text_classification(self, text: str, model: str) -> list[dict]:
        return _scores("totally-not-a-classification")  # unparseable → raises

    def zero_shot(self, text: str, labels: list[str], model: str) -> list[dict]:
        return _scores(42)  # unparseable → raises

    def audio_classification(self, audio: bytes, model: str) -> list[dict]:
        return _scores({"prediction": "speech"})  # unparseable → raises

    def visual_question_answering(self, image: bytes, question: str, model: str) -> dict:
        return _qa_top("not-a-qa-element")  # unparseable → raises

    def document_question_answering(self, image: bytes, question: str, model: str) -> dict:
        return _qa_top([{"text": "no answer key"}])  # unparseable → raises


def _mal() -> HfClient:
    # only the classifier/QA methods matter here; cast to the seam so the tool
    # constructors (which want a full HfClient) type-check.
    return cast(HfClient, _MalformedClient())


async def test_classify_surfaces_malformed_output() -> None:
    tool = Classify("distilbert/sst2", client=_mal())
    out = await tool(None, text="great!")
    # Old code coerced to {"top": None} (a clean-looking empty). Now the failure is
    # visible: the loop's error convention (obs["error"]) fires downstream.
    assert "error" in out and "unparseable" in out
    assert "top" not in out  # not coerced to a default label
    assert out["model"] == "distilbert/sst2"


async def test_zero_shot_and_audio_classify_surface_malformed() -> None:
    zs = await ZeroShotClassify("bart-mnli", client=_mal())(
        None, text="x", labels=["a", "b"]
    )
    assert "error" in zs and "unparseable" in zs
    ac = await ClassifyAudio("MIT/ast", client=_mal())(None, data_b64=_B64)
    assert "error" in ac and "unparseable" in ac


async def test_qa_tools_surface_malformed_output() -> None:
    vqa = await AskImage("vilt", client=_mal())(None, question="q?", data_b64=_B64)
    assert "error" in vqa and "unparseable" in vqa
    assert "answer" not in vqa  # not coerced to {"answer": ""}
    doc = await AskDocument("layoutlm", client=_mal())(
        None, question="q?", data_b64=_B64
    )
    assert "error" in doc and "unparseable" in doc


def test_classifier_detector_surfaces_malformed_instead_of_clean_pass() -> None:
    # A detector previously saw [] from a malformed response and returned None (a
    # CLEAN pass — the coerced-away failure was invisible). Now the normaliser
    # raises, so inspect() raises and the loop surfaces it (harness.check.crashed)
    # rather than a silent, misleading "no flag".
    det = HfClassifierDetector(
        _mal(), "m", escalate_on=["toxic"], candidate_labels=None
    )
    ctx = RunContext(spec=None, observation={"text": "some content to classify"})
    with pytest.raises(MalformedModelOutput):
        det.inspect(ctx)


# --- F26: backend + supply-chain policy + pins configurable from config --------


def test_default_backend_is_hosted_router_unchanged() -> None:
    # Unset config ⇒ identical to before: hosted InferenceClientBackend, net+router.
    tool = Classify("distilbert/sst2")
    assert isinstance(tool._backend, InferenceClientBackend)
    assert tool.capabilities == frozenset({CAP_NET})
    assert tool.egress == frozenset({"router.huggingface.co"})


def test_local_backend_selected_from_config_threads_policy_and_pins() -> None:
    # backend='local' builds a PipelineBackend (no egress) and threads the
    # supply-chain policy + per-model pins (cluster-5) through to it — from config,
    # no client injection, no transformers import (the pipeline is lazy).
    policy = SupplyChainPolicy(require_pinned_revision=False)
    pins = {"acme/clf": ModelPin(repo_id="acme/clf", revision="a" * 40)}
    tool = Classify("acme/clf", backend="local", policy=policy, pins=pins)
    assert isinstance(tool._backend, PipelineBackend)
    # local ⇒ zero egress envelope
    assert tool.capabilities == frozenset()
    assert tool.egress == frozenset()
    # policy + pins reached the backend construction
    assert tool._backend._policy is policy
    assert tool._backend._pin_for("acme/clf").revision == "a" * 40


def test_hosted_rejects_supply_chain_args_loudly() -> None:
    # The supply-chain policy/pins only apply to the local pipeline; asking for them
    # on the hosted router is a config error, surfaced rather than silently dropped.
    with pytest.raises(ValueError, match="local PipelineBackend only"):
        Classify("m", backend="hosted", policy=SupplyChainPolicy())


def test_unknown_backend_selector_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown backend"):
        Classify("m", backend="serverless")  # not 'hosted' or 'local'


def test_explicit_client_still_wins_over_backend_selector(fake_client) -> None:
    # The injected client seam (the test/DI path) takes precedence, unchanged.
    tool = Classify("m", client=fake_client, backend="local")
    assert tool._c() is fake_client

