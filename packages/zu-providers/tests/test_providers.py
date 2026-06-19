"""Build step 7 — the real ModelProvider adapters: anthropic + openai-compatible.

The headline contract: **both adapters pass one shared checklist, so they behave
identically.** Each is exercised offline against its *real* SDK — an injected
client wired to an ``httpx.MockTransport`` returns canned provider JSON, so the
adapter's translation and the SDK's own parsing both run, with no network. A
live call against each API is opt-in (env-gated) so it never blocks CI.
"""

from __future__ import annotations

import json
import os

import anthropic
import httpx
import openai
import pytest

from zu_core.ports import Finish, ModelRequest
from zu_providers._messages import to_anthropic_messages, to_openai_messages
from zu_providers.anthropic import AnthropicProvider
from zu_providers.openai_compatible import OpenAICompatibleProvider

_TOOL = {
    "name": "http_fetch",
    "description": "Fetch a URL.",
    "parameters": {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
}

# Canned provider responses for each checklist scenario, in each wire format.
_ANTHROPIC: dict[str, dict] = {
    "text": {
        "id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-4-8",
        "content": [{"type": "text", "text": "hello world"}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 5},
    },
    "tool": {
        "id": "msg_2", "type": "message", "role": "assistant", "model": "claude-opus-4-8",
        "content": [{"type": "tool_use", "id": "toolu_x", "name": "http_fetch", "input": {"url": "http://e.test/"}}],
        "stop_reason": "tool_use", "stop_sequence": None,
        "usage": {"input_tokens": 12, "output_tokens": 7},
    },
    "length": {
        "id": "msg_3", "type": "message", "role": "assistant", "model": "claude-opus-4-8",
        "content": [{"type": "text", "text": "partial"}],
        "stop_reason": "max_tokens", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 4},
    },
}
_OPENAI: dict[str, dict] = {
    "text": {
        "id": "c1", "object": "chat.completion", "created": 0, "model": "gpt-x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello world"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
    },
    "tool": {
        "id": "c2", "object": "chat.completion", "created": 0, "model": "gpt-x",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant", "content": None,
                "tool_calls": [{
                    "id": "call_x", "type": "function",
                    "function": {"name": "http_fetch", "arguments": "{\"url\": \"http://e.test/\"}"},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
    },
    "length": {
        "id": "c3", "object": "chat.completion", "created": 0, "model": "gpt-x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "partial"}, "finish_reason": "length"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
    },
}


def _mock_transport(payload: dict, captured: list) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def make_anthropic(scenario: str, captured: list) -> AnthropicProvider:
    client = anthropic.AsyncAnthropic(
        api_key="test", http_client=httpx.AsyncClient(transport=_mock_transport(_ANTHROPIC[scenario], captured))
    )
    return AnthropicProvider(client=client)


def make_openai(scenario: str, captured: list) -> OpenAICompatibleProvider:
    client = openai.AsyncOpenAI(
        api_key="test",
        base_url="http://test.local/v1",
        http_client=httpx.AsyncClient(transport=_mock_transport(_OPENAI[scenario], captured)),
    )
    return OpenAICompatibleProvider(model="gpt-x", client=client)


_PROVIDERS = [
    pytest.param(make_anthropic, id="anthropic"),
    pytest.param(make_openai, id="openai-compatible"),
]


# --- the shared checklist: identical neutral behaviour from both adapters -----


@pytest.mark.parametrize("make", _PROVIDERS)
async def test_text_finalize(make) -> None:
    p = make("text", [])
    r = await p.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))
    assert r.finish is Finish.STOP
    assert r.text == "hello world"
    assert r.tool_calls == []
    assert r.usage["input_tokens"] == 10 and r.usage["output_tokens"] == 5


@pytest.mark.parametrize("make", _PROVIDERS)
async def test_tool_call(make) -> None:
    p = make("tool", [])
    r = await p.complete(ModelRequest(messages=[{"role": "user", "content": "fetch"}], tools=[_TOOL]))
    assert r.finish is Finish.TOOL_CALLS
    assert len(r.tool_calls) == 1
    assert r.tool_calls[0].name == "http_fetch"
    assert r.tool_calls[0].args == {"url": "http://e.test/"}  # parsed to a dict, both ways
    assert r.text is None


@pytest.mark.parametrize("make", _PROVIDERS)
async def test_length_is_finish_length(make) -> None:
    p = make("length", [])
    r = await p.complete(ModelRequest(messages=[{"role": "user", "content": "x"}]))
    assert r.finish is Finish.LENGTH


@pytest.mark.parametrize("make", _PROVIDERS)
async def test_capabilities_present(make) -> None:
    assert make("text", []).capabilities.native_tools is True


async def test_openai_usage_includes_total_tokens() -> None:
    # The neutral usage dict the cost projection reads carries total_tokens for
    # the OpenAI shape (Anthropic omits it; the loop sums input+output either way).
    r = await make_openai("text", []).complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))
    assert r.usage["total_tokens"] == 15


def test_api_key_resolution_prefers_explicit_then_env(monkeypatch) -> None:
    # A directly-passed key works with no env var set; without either, the
    # adapter raises a clear error rather than calling the API with no auth.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    AnthropicProvider(model="claude-x", api_key="sk-explicit")._ensure_client()  # no raise

    with pytest.raises(RuntimeError, match="no Anthropic API key"):
        AnthropicProvider(model="claude-x")._ensure_client()

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-from-env")
    AnthropicProvider(model="claude-x")._ensure_client()  # resolves from env


async def test_native_tools_false_raises_not_implemented() -> None:
    # The prompt-based tool fallback for non-native-tool models is deferred;
    # the adapter must raise clearly, never silently guess.
    p = OpenAICompatibleProvider(model="local", native_tools=False, client=object())
    with pytest.raises(NotImplementedError):
        await p.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))


def test_orphan_tool_result_raises_not_silent() -> None:
    # A tool result with no preceding tool call is a malformed history; both
    # translators must fail loudly here rather than fabricate an id that the
    # provider would reject downstream as an opaque 400.
    bad = [{"role": "tool", "name": "http_fetch", "content": "{}"}]
    with pytest.raises(ValueError):
        to_anthropic_messages(bad)
    with pytest.raises(ValueError):
        to_openai_messages(bad)


# --- request translation (provider-specific wire shapes) ----------------------


async def test_anthropic_request_translation() -> None:
    captured: list = []
    p = make_anthropic("text", captured)
    await p.complete(
        ModelRequest(
            messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            tools=[_TOOL],
        )
    )
    body = captured[0]
    assert body["system"] == "sys"  # system lifted out of messages
    assert body["messages"] == [{"role": "user", "content": "hi"}]
    assert body["max_tokens"] == 4096  # adapter default
    assert body["tools"][0]["name"] == "http_fetch"
    assert "input_schema" in body["tools"][0]  # parameters -> input_schema


async def test_openai_request_translation() -> None:
    captured: list = []
    p = make_openai("text", captured)
    await p.complete(
        ModelRequest(
            messages=[{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}],
            tools=[_TOOL],
        )
    )
    body = captured[0]
    assert body["messages"][0] == {"role": "system", "content": "sys"}  # system stays inline
    assert body["tools"][0]["type"] == "function"
    assert body["tools"][0]["function"]["name"] == "http_fetch"


# --- the id-matching translation (pure, no SDK) -------------------------------

_TOOL_HISTORY: list[dict] = [
    {"role": "user", "content": "q"},
    {"role": "assistant", "tool_calls": [{"name": "http_fetch", "args": {"url": "u"}}]},
    {"role": "tool", "name": "http_fetch", "content": "{\"html\": \"x\"}"},
]


def test_to_anthropic_matches_synthesised_tool_ids() -> None:
    _, out = to_anthropic_messages(_TOOL_HISTORY)
    use_id = out[1]["content"][0]["id"]
    result_block = out[2]["content"][0]
    assert result_block["type"] == "tool_result"
    assert result_block["tool_use_id"] == use_id  # result references the same id


def test_to_openai_matches_synthesised_tool_ids() -> None:
    out = to_openai_messages(_TOOL_HISTORY)
    call_id = out[1]["tool_calls"][0]["id"]
    assert out[2]["role"] == "tool"
    assert out[2]["tool_call_id"] == call_id


# --- the adapter drives the real loop (full assistant/tool history) -----------


async def test_anthropic_adapter_drives_the_loop() -> None:
    # The checklist uses simple messages; this proves the loop's full neutral
    # history (system + user -> tool_use -> assistant tool_calls + tool result ->
    # finalise) round-trips through the adapter's translation, end to end.
    from zu_core.bus import EventBus
    from zu_core.contracts import Status, TaskSpec
    from zu_core.loop import run_task
    from zu_core.registry import Registry
    from zu_tools.fetch import HttpFetch

    page = "<html><body><span class='price'>$9.00</span></body></html>"

    def fetch_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page)

    reg = Registry()
    reg.register(
        "tools", "http_fetch", HttpFetch(allow_private=True, transport=httpx.MockTransport(fetch_handler))
    )

    # Stateful model mock: first call asks to fetch, second call finalises.
    turn = {"n": 0}

    def model_handler(request: httpx.Request) -> httpx.Response:
        turn["n"] += 1
        if turn["n"] == 1:
            payload = {
                "id": "m1", "type": "message", "role": "assistant", "model": "claude-opus-4-8",
                "content": [{"type": "tool_use", "id": "toolu_a", "name": "http_fetch", "input": {"url": "http://x.test/"}}],
                "stop_reason": "tool_use", "stop_sequence": None,
                "usage": {"input_tokens": 20, "output_tokens": 8},
            }
        else:
            payload = {
                "id": "m2", "type": "message", "role": "assistant", "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "{\"price\": \"$9.00\"}"}],
                "stop_reason": "end_turn", "stop_sequence": None,
                "usage": {"input_tokens": 40, "output_tokens": 12},
            }
        return httpx.Response(200, json=payload)

    client = anthropic.AsyncAnthropic(
        api_key="test", http_client=httpx.AsyncClient(transport=httpx.MockTransport(model_handler))
    )
    provider = AnthropicProvider(client=client)

    result = await run_task(TaskSpec(query="get the price"), provider, reg, EventBus())
    assert result.status == Status.SUCCESS
    assert result.value == {"price": "$9.00"}
    assert turn["n"] == 2  # the loop drove two model turns through the adapter


# --- opt-in live calls (skipped unless env-gated) -----------------------------


@pytest.mark.skipif(not os.environ.get("ZU_LIVE_ANTHROPIC"), reason="set ZU_LIVE_ANTHROPIC=1 + ANTHROPIC_API_KEY")
async def test_live_anthropic() -> None:
    p = AnthropicProvider()
    r = await p.complete(
        ModelRequest(messages=[{"role": "user", "content": "Reply with the single word: pong"}], params={"max_tokens": 16})
    )
    assert r.text is not None and "pong" in r.text.lower()


@pytest.mark.skipif(not os.environ.get("ZU_LIVE_OPENAI"), reason="set ZU_LIVE_OPENAI=1 + OPENAI_API_KEY (+ OPENAI_BASE_URL)")
async def test_live_openai() -> None:
    p = OpenAICompatibleProvider(model=os.environ.get("ZU_LIVE_OPENAI_MODEL", "gpt-4o-mini"))
    r = await p.complete(
        ModelRequest(messages=[{"role": "user", "content": "Reply with the single word: pong"}], params={"max_tokens": 16})
    )
    assert r.text is not None and "pong" in r.text.lower()
