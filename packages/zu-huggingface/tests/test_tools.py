"""HuggingFace task models as Zu Tools (§8.5) — offline, against a fake client.

Proves each tool produces the loop-friendly observation shape, calls the right
client method with the right model, and derives its capability envelope from the
backend (hosted ⇒ net + router; local ⇒ nothing).
"""

from __future__ import annotations

import base64

from zu_core.ports import CAP_NET
from zu_huggingface import (
    Classify,
    DetectObjects,
    Embed,
    ImageToText,
    Summarize,
    Transcribe,
    Translate,
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

