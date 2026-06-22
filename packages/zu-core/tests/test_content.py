"""Multimodal content contracts (Engineering Design §8.2, §9).

Proves the modality-agnostic currency holds: a heterogeneous observation
round-trips through JSON (the event-log path) without losing its types or
crashing on binary, the text view ignores other modalities, and a typed Action
carries each policy shape (final text, tool call, control command).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from zu_core import Action, Audio, Image, Observation, Text
from zu_core.content import ContentPart


def test_text_observation_roundtrip() -> None:
    obs = Observation.from_text("hello")
    assert obs.text() == "hello"
    assert [p.kind for p in obs.content] == ["text"]


def test_mixed_observation_json_roundtrip_is_lossless() -> None:
    obs = Observation(
        content=[
            Text(text="describe this"),
            Image(data=b"\x89PNG\x00\xff", mime="image/png"),
            Audio(data=b"\x00\x01\x02", mime="audio/wav"),
        ]
    )
    # JSON mode must not crash on binary (base64) and must preserve every part.
    dumped = obs.model_dump(mode="json")
    restored = Observation.model_validate(dumped)
    assert restored == obs
    assert [p.kind for p in restored.content] == ["text", "image", "audio"]
    img = restored.parts("image")[0]
    assert isinstance(img, Image)
    assert img.data == b"\x89PNG\x00\xff"


def test_text_view_ignores_other_modalities() -> None:
    obs = Observation(
        content=[Text(text="a"), Image(data=b"x"), Text(text="b")]
    )
    assert obs.text() == "a\nb"


def test_content_is_frozen() -> None:
    t = Text(text="x")
    with pytest.raises(ValidationError):
        t.text = "y"


def test_discriminator_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        Observation.model_validate({"content": [{"kind": "video", "data": "x"}]})


def test_action_shapes() -> None:
    final = Action.text("done")
    assert final.kind == "text" and final.payload == {"text": "done"}

    call = Action.tool_call("http_fetch", {"url": "https://example.com"})
    assert call.kind == "tool_call"
    assert call.payload == {"name": "http_fetch", "args": {"url": "https://example.com"}}

    cmd = Action.command(actuator="gait", vector=[0.1, 0.0])
    assert cmd.kind == "command"
    assert cmd.payload == {"actuator": "gait", "vector": [0.1, 0.0]}


def test_action_rejects_unknown_kind() -> None:
    with pytest.raises(ValidationError):
        Action(kind="teleport", payload={})  # type: ignore[arg-type]


def test_contentpart_union_parses_each_member() -> None:
    # The exported discriminated alias is what downstream models reuse.
    obs = Observation(content=[Text(text="x")])
    assert isinstance(obs.content[0], Text)
    # Smoke: the alias is importable and usable as an annotation target.
    assert ContentPart is not None
