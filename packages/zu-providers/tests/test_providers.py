"""Build step 7 — the real ModelProvider adapters: anthropic + openai-compatible.

The headline contract: **both adapters pass one shared checklist, so they behave
identically.** Each is exercised offline against its *real* SDK — an injected
client wired to an ``httpx.MockTransport`` returns canned provider JSON, so the
adapter's translation and the SDK's own parsing both run, with no network. A
live call against each API is opt-in (env-gated) so it never blocks CI.
"""

from __future__ import annotations

import base64 as _base64
import json
import os

import anthropic
import httpx
import openai
import pytest

from zu_core.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
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


@pytest.mark.parametrize("make", _PROVIDERS)
async def test_usage_shape_is_normalised(make) -> None:
    # The neutral usage dict the cost projection reads carries the SAME keys from
    # both adapters: input/output/total. OpenAI returns a total on the wire;
    # Anthropic doesn't, so the adapter computes it (input + output) — either way
    # the cost projection sees one shape.
    r = await make("text", []).complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))
    assert r.usage["input_tokens"] == 10
    assert r.usage["output_tokens"] == 5
    assert r.usage["total_tokens"] == 15  # 10 + 5, whether the API gave it or not


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


async def test_openai_empty_choices_is_no_answer_not_crash() -> None:
    # Some OpenAI-compatible servers (vLLM/Ollama/proxies) return choices: [] on
    # certain errors/policy stops. The adapter must surface that as a no-answer
    # STOP, never IndexError on choices[0].
    captured: list = []
    payload = {
        "id": "c0", "object": "chat.completion", "created": 0, "model": "gpt-x",
        "choices": [],
        "usage": {"prompt_tokens": 3, "completion_tokens": 0, "total_tokens": 3},
    }
    client = openai.AsyncOpenAI(
        api_key="test", base_url="http://test.local/v1",
        http_client=httpx.AsyncClient(transport=_mock_transport(payload, captured)),
    )
    p = OpenAICompatibleProvider(model="gpt-x", client=client)
    resp = await p.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))
    assert resp.text is None
    assert resp.tool_calls == []
    assert resp.finish is Finish.STOP
    assert resp.usage["total_tokens"] == 3  # usage still captured


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


# --- #65 F28: sampling params threaded through to BOTH adapters' SDK calls -----


async def test_anthropic_threads_sampling_params() -> None:
    # temperature/top_p/top_k/stop are carried onto the Anthropic wire body
    # (stop -> stop_sequences); ``seed`` is dropped (no Messages API field for it).
    # On old code only max_tokens was honoured and these were silently dropped.
    captured: list = []
    p = make_anthropic("text", captured)
    await p.complete(
        ModelRequest(
            messages=[{"role": "user", "content": "hi"}],
            params={
                "temperature": 0.3,
                "top_p": 0.9,
                "top_k": 40,
                "stop": ["STOP"],
                "seed": 123,  # unsupported by this vendor -> omitted
            },
        )
    )
    body = captured[0]
    assert body["temperature"] == 0.3
    assert body["top_p"] == 0.9
    assert body["top_k"] == 40
    assert body["stop_sequences"] == ["STOP"]  # neutral 'stop' -> vendor wire name
    assert "seed" not in body  # dropped: Anthropic has no seed field


async def test_openai_threads_sampling_params() -> None:
    # temperature/top_p/stop/seed are carried onto the Chat Completions body;
    # ``top_k`` is dropped (no Chat Completions field for it). Mirror of the
    # anthropic test with this vendor's supported set.
    captured: list = []
    p = make_openai("text", captured)
    await p.complete(
        ModelRequest(
            messages=[{"role": "user", "content": "hi"}],
            params={
                "temperature": 0.3,
                "top_p": 0.9,
                "stop": ["STOP"],
                "seed": 123,
                "top_k": 40,  # unsupported by this vendor -> omitted
            },
        )
    )
    body = captured[0]
    assert body["temperature"] == 0.3
    assert body["top_p"] == 0.9
    assert body["stop"] == ["STOP"]
    assert body["seed"] == 123
    assert "top_k" not in body  # dropped: Chat Completions has no top_k field


@pytest.mark.parametrize("make", _PROVIDERS)
async def test_unset_sampling_params_are_omitted_not_none(make) -> None:
    # A request with no sampling params must not send temperature/top_p/etc. at
    # all — never as None. (max_tokens is threaded separately and may appear.)
    captured: list = []
    p = make("text", captured)
    await p.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))
    body = captured[0]
    for k in ("temperature", "top_p", "top_k", "stop", "stop_sequences", "seed"):
        assert k not in body


# --- #65 F29: openai-compatible adapter guards a partial/None usage object -----


async def test_openai_partial_usage_does_not_crash_or_misreport() -> None:
    # A partial usage object (only prompt_tokens; completion/total missing) must
    # degrade to the normalised shape with 0-filled fields and a computed total —
    # the same defensive handling the anthropic adapter applies — not raise
    # AttributeError or report None tokens. On old code ``raw.completion_tokens``
    # / ``raw.total_tokens`` would blow up here.
    captured: list = []
    payload = {
        "id": "c9", "object": "chat.completion", "created": 0, "model": "gpt-x",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 7},  # completion_tokens / total_tokens absent
    }
    client = openai.AsyncOpenAI(
        api_key="test", base_url="http://test.local/v1",
        http_client=httpx.AsyncClient(transport=_mock_transport(payload, captured)),
    )
    p = OpenAICompatibleProvider(model="gpt-x", client=client)
    resp = await p.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))
    assert resp.usage["input_tokens"] == 7
    assert resp.usage["output_tokens"] == 0  # missing -> 0, not None
    assert resp.usage["total_tokens"] == 7  # computed from the parts


# --- #65 F33: the adapter's repr/str must not leak the resolved API key --------


@pytest.mark.parametrize(
    "make_provider",
    [
        pytest.param(lambda: AnthropicProvider(api_key="sk-super-secret"), id="anthropic"),
        pytest.param(
            lambda: OpenAICompatibleProvider(model="gpt-x", api_key="sk-super-secret"),
            id="openai-compatible",
        ),
    ],
)
def test_repr_does_not_leak_api_key(make_provider) -> None:
    p = make_provider()
    for rendered in (repr(p), str(p), f"{p!r}", f"{p}", format(p)):
        assert "sk-super-secret" not in rendered
    # ...and the key must not sit under a PUBLIC attribute name (the surface a
    # naive ``{k: v for k, v}`` config dump or an ORM/pydantic serializer walks).
    # It lives under a private ``_api_key`` so it stays out of those paths.
    assert not any(v == "sk-super-secret" for k, v in vars(p).items() if not k.startswith("_"))


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


# An assistant turn that reasons *and* calls a tool: the text must survive into
# both wire formats (regression: it used to be silently dropped on replay).
_TOOL_HISTORY_WITH_TEXT: list[dict] = [
    {"role": "user", "content": "q"},
    {"role": "assistant", "content": "I'll fetch the page first.",
     "tool_calls": [{"name": "http_fetch", "args": {"url": "u"}}]},
    {"role": "tool", "name": "http_fetch", "content": "{\"html\": \"x\"}"},
]


def test_anthropic_preserves_assistant_text_with_tool_calls() -> None:
    _, out = to_anthropic_messages(_TOOL_HISTORY_WITH_TEXT)
    blocks = out[1]["content"]
    assert blocks[0] == {"type": "text", "text": "I'll fetch the page first."}
    assert blocks[1]["type"] == "tool_use"  # text leads, tool_use follows


def test_openai_preserves_assistant_text_with_tool_calls() -> None:
    out = to_openai_messages(_TOOL_HISTORY_WITH_TEXT)
    assert out[1]["content"] == "I'll fetch the page first."
    assert out[1]["tool_calls"][0]["function"]["name"] == "http_fetch"


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


# --- #59: neutral image block -> per-provider wire shape at the adapter --------

_IMG_RAW = b"\x89PNG"
_IMG_B64 = _base64.b64encode(_IMG_RAW).decode()
# One neutral user turn carrying a neutral image block (what LlmPolicy emits).
_IMAGE_HISTORY: list[dict] = [
    {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image", "mime": "image/png", "data": _IMG_B64},
        ],
    }
]


def test_anthropic_translates_neutral_image_to_source_block() -> None:
    # The Anthropic adapter translates the neutral image block into Anthropic's
    # {"type":"image","source":{"type":"base64",...}} wire shape — no OpenAI
    # image_url anywhere.
    _, out = to_anthropic_messages(_IMAGE_HISTORY)
    content = out[0]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": _IMG_B64},
    }
    assert "image_url" not in content[1]


def test_openai_translates_neutral_image_to_image_url_block() -> None:
    # The OpenAI-compatible adapter translates the neutral image block into the
    # existing image_url data-URL shape.
    out = to_openai_messages(_IMAGE_HISTORY)
    content = out[0]["content"]
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1] == {
        "type": "image_url",
        "image_url": {"url": f"data:image/png;base64,{_IMG_B64}"},
    }


# --- #60: SDK exceptions translated to neutral zu_core errors at the port ------


class _Boom:
    """A stub client whose create() raises a preset SDK exception — proves the
    adapter translates at the port boundary with no network."""

    def __init__(self, exc: Exception, *, anthropic_shape: bool) -> None:
        self._exc = exc
        if anthropic_shape:
            self.messages = _RaiseOn(exc, "create")
        else:
            self.chat = _Chat(exc)


class _RaiseOn:
    def __init__(self, exc: Exception, attr: str) -> None:
        self._exc = exc
        setattr(self, attr, self._raise)

    async def _raise(self, **kwargs):  # noqa: ANN003
        raise self._exc


class _Chat:
    def __init__(self, exc: Exception) -> None:
        self.completions = _RaiseOn(exc, "create")


def _anthropic_exc(name: str) -> Exception:
    """Construct a representative anthropic SDK exception of the given class."""
    import httpx

    req = httpx.Request("POST", "http://test.local/")
    if name == "auth":
        return anthropic.AuthenticationError(
            "bad key", response=httpx.Response(401, request=req), body=None
        )
    if name == "rate":
        return anthropic.RateLimitError(
            "slow down", response=httpx.Response(429, request=req), body=None
        )
    if name == "timeout":
        return anthropic.APITimeoutError(request=req)
    if name == "connection":
        return anthropic.APIConnectionError(message="no route", request=req)
    return RuntimeError("something unexpected")  # unknown -> base ProviderError


def _openai_exc(name: str) -> Exception:
    import httpx

    req = httpx.Request("POST", "http://test.local/")
    if name == "auth":
        return openai.AuthenticationError(
            "bad key", response=httpx.Response(401, request=req), body=None
        )
    if name == "rate":
        return openai.RateLimitError(
            "slow down", response=httpx.Response(429, request=req), body=None
        )
    if name == "timeout":
        return openai.APITimeoutError(request=req)
    if name == "connection":
        return openai.APIConnectionError(message="no route", request=req)
    return RuntimeError("something unexpected")


def _make_anthropic_raising(exc: Exception) -> AnthropicProvider:
    return AnthropicProvider(client=_Boom(exc, anthropic_shape=True))


def _make_openai_raising(exc: Exception) -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(model="gpt-x", client=_Boom(exc, anthropic_shape=False))


# (vendor exception kind -> expected neutral type), proven identical for both ports.
_ERROR_CASES = [
    pytest.param("auth", ProviderAuthError, id="auth"),
    pytest.param("rate", ProviderRateLimited, id="rate-limit"),
    pytest.param("timeout", ProviderTimeout, id="timeout"),
    pytest.param("connection", ProviderUnavailable, id="connection"),
    pytest.param("unknown", ProviderError, id="unknown-base"),
]

_ERROR_PROVIDERS = [
    pytest.param(_make_anthropic_raising, _anthropic_exc, id="anthropic"),
    pytest.param(_make_openai_raising, _openai_exc, id="openai-compatible"),
]


@pytest.mark.parametrize("make_raising, make_exc", _ERROR_PROVIDERS)
@pytest.mark.parametrize("kind, neutral", _ERROR_CASES)
async def test_sdk_exception_translated_to_neutral_error(
    make_raising, make_exc, kind, neutral
) -> None:
    sdk_exc = make_exc(kind)
    provider = make_raising(sdk_exc)
    with pytest.raises(neutral) as ei:
        await provider.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))
    # the neutral error is raised, and the raw SDK cause is chained for diagnostics
    assert ei.value.__cause__ is sdk_exc
    # nothing vendor-specific escapes the port: every neutral type IS a ProviderError
    assert isinstance(ei.value, ProviderError)


# --- opt-in live calls (@pytest.mark.live + a real key; run with --run-live) ---


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
async def test_live_anthropic() -> None:
    p = AnthropicProvider()
    r = await p.complete(
        ModelRequest(messages=[{"role": "user", "content": "Reply with the single word: pong"}], params={"max_tokens": 16})
    )
    assert r.text is not None and "pong" in r.text.lower()


@pytest.mark.live
@pytest.mark.skipif(
    not (os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_BASE_URL")),
    reason="needs OPENAI_API_KEY or OPENAI_BASE_URL",
)
async def test_live_openai() -> None:
    p = OpenAICompatibleProvider(model=os.environ.get("ZU_LIVE_OPENAI_MODEL", "gpt-4o-mini"))
    r = await p.complete(
        ModelRequest(messages=[{"role": "user", "content": "Reply with the single word: pong"}], params={"max_tokens": 16})
    )
    assert r.text is not None and "pong" in r.text.lower()
