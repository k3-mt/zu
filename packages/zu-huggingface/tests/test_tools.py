"""HuggingFace task models as Zu Tools (§8.5) — offline, against a fake client.

Proves each tool produces the loop-friendly observation shape, calls the right
client method with the right model, and derives its capability envelope from the
backend (hosted ⇒ net + router; local ⇒ nothing).
"""

from __future__ import annotations

import base64

from zu_core.ports import CAP_NET
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
    assert "predicted_depth" not in out  # kept compact/JSON-safe
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

