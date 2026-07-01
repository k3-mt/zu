"""LlmPolicy — bridging an LLM ModelProvider onto the generalised Policy port.

Proves the bridge maps both response shapes (a chosen tool call → a tool_call
Action; plain text → a text Action), forwards the tool specs, and carries the
observation's typed content into the request (text collapsed to a string;
images as base64 blocks for a vision provider). No network — a fake provider.
"""

from __future__ import annotations

from zu_core import Policy
from zu_core.content import Image, Observation, Text
from zu_core.ports import Capabilities, Finish, ModelRequest, ModelResponse, ToolCall, ToolSpec
from zu_providers.llm_policy import LlmPolicy


class _FakeProvider:
    capabilities = Capabilities(vision=True)
    model = "fake-1"

    def __init__(self, response: ModelResponse) -> None:
        self._response = response
        self.seen: ModelRequest | None = None

    async def complete(self, req: ModelRequest) -> ModelResponse:
        self.seen = req
        return self._response


def test_llm_policy_satisfies_the_policy_protocol() -> None:
    p = LlmPolicy(_FakeProvider(ModelResponse(text="hi")))
    assert isinstance(p, Policy)
    assert p.model == "fake-1"
    assert p.capabilities.vision is True


async def test_tool_call_response_maps_to_tool_call_action() -> None:
    resp = ModelResponse(
        tool_calls=[ToolCall(name="http_fetch", args={"url": "https://x"})],
        finish=Finish.TOOL_CALLS,
    )
    provider = _FakeProvider(resp)
    policy = LlmPolicy(provider)
    tools = [ToolSpec(name="http_fetch", description="fetch", json_schema={"name": "http_fetch"})]
    action = await policy.act(Observation.from_text("get the page"), tools)
    assert action.kind == "tool_call"
    assert action.payload == {"name": "http_fetch", "args": {"url": "https://x"}}
    # the tool specs were forwarded as the request's tools
    assert provider.seen is not None
    assert provider.seen.tools == [{"name": "http_fetch"}]


async def test_text_response_maps_to_text_action() -> None:
    policy = LlmPolicy(_FakeProvider(ModelResponse(text="the answer is 42")))
    action = await policy.act(Observation.from_text("question?"), [])
    assert action.kind == "text"
    assert action.payload == {"text": "the answer is 42"}


async def test_text_only_observation_collapses_to_a_string() -> None:
    provider = _FakeProvider(ModelResponse(text="ok"))
    await LlmPolicy(provider, system="be terse").act(Observation.from_text("hello"), [])
    msgs = provider.seen.messages  # type: ignore[union-attr]
    assert msgs[0] == {"role": "system", "content": "be terse"}
    assert msgs[1] == {"role": "user", "content": "hello"}


async def test_image_observation_becomes_neutral_image_blocks() -> None:
    # The policy-neutral layer emits a NEUTRAL image block (mime + base64 data),
    # NOT a vendor wire-format. Each provider adapter translates it to its own
    # shape (image_url / image+source) at the adapter boundary — proven in
    # test_providers.py. This pins that the neutral seam carries no OpenAI shape.
    provider = _FakeProvider(ModelResponse(text="a cat"))
    obs = Observation(content=[Text(text="what is this?"), Image(data=b"\x89PNG", mime="image/png")])
    await LlmPolicy(provider).act(obs, [])
    content = provider.seen.messages[-1]["content"]  # type: ignore[union-attr]
    assert isinstance(content, list)
    kinds = [c["type"] for c in content]
    assert kinds == ["text", "image"]  # neutral, not "image_url"
    import base64

    assert content[1] == {
        "type": "image",
        "mime": "image/png",
        "data": base64.b64encode(b"\x89PNG").decode(),
    }
    # No vendor-specific key leaked into the neutral layer.
    assert "image_url" not in content[1] and "source" not in content[1]


async def test_image_to_vision_incapable_provider_raises_clear_error() -> None:
    # A provider that advertises no vision must never receive image blocks it
    # cannot encode; the policy gates locally and clearly before any request.
    class _NoVision(_FakeProvider):
        capabilities = Capabilities(vision=False)

    provider = _NoVision(ModelResponse(text="x"))
    obs = Observation(content=[Text(text="hi"), Image(data=b"\x89PNG", mime="image/png")])
    import pytest

    with pytest.raises(ValueError, match="vision"):
        await LlmPolicy(provider).act(obs, [])
