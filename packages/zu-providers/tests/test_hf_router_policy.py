"""The HuggingFace policy path (§6.4) — the OpenAI-compatible adapter against the
HF *chat* surfaces, by SHAPE, offline.

A HuggingFace chat / vision-language model as the **policy** needs no new code:
it is the existing ``openai-compatible`` provider pointed at a HuggingFace base
URL with ``api_key_env=HF_TOKEN``. The router's ``/v1``, an Inference Endpoint's
``/v1``, and a local vLLM ``/v1`` are the *same adapter + config* — only the base
URL differs. These tests prove that with an ``httpx.MockTransport`` serving the
OpenAI response shape (no network, no key): the request is built correctly
(path, bearer from HF_TOKEN, body), the response parses identically, and a VLM
policy (image in the chat request) rides the same adapter.
"""

from __future__ import annotations

import json

import httpx
import openai
import pytest

from zu_core.ports import Finish, ModelRequest
from zu_providers.openai_compatible import OpenAICompatibleProvider

_HF_BASE = "https://router.huggingface.co/v1"
_HF_MODEL = "meta-llama/Llama-3.1-8B-Instruct"

_TOOL = {
    "name": "http_fetch",
    "description": "Fetch a URL.",
    "parameters": {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
    },
}

_OPENAI_TEXT = {
    "id": "c1", "object": "chat.completion", "created": 0, "model": _HF_MODEL,
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "hello world"},
                 "finish_reason": "stop"}],
    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
}
_OPENAI_TOOL = {
    "id": "c2", "object": "chat.completion", "created": 0, "model": _HF_MODEL,
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
}


def _capturing_transport(payload: dict, captured: list[httpx.Request]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json=payload)

    return httpx.MockTransport(handler)


def _provider(token: str, payload: dict, captured: list, *, base_url: str = _HF_BASE):
    # Inject the real AsyncOpenAI so the SDK's request-building and parsing both
    # run; the bearer is derived from the token exactly as _ensure_client would.
    client = openai.AsyncOpenAI(
        api_key=token,
        base_url=base_url,
        http_client=httpx.AsyncClient(transport=_capturing_transport(payload, captured)),
    )
    return OpenAICompatibleProvider(
        model=_HF_MODEL, base_url=base_url, api_key_env="HF_TOKEN", client=client
    )


async def test_hf_router_request_shape_and_auth(monkeypatch) -> None:
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    captured: list[httpx.Request] = []
    p = _provider("hf_secret", _OPENAI_TEXT, captured)
    await p.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}], tools=[_TOOL]))
    req = captured[0]
    # path is /v1/chat/completions appended to the HF router base_url
    assert str(req.url) == "https://router.huggingface.co/v1/chat/completions"
    # auth derived from HF_TOKEN
    assert req.headers["authorization"] == "Bearer hf_secret"
    body = json.loads(req.content)
    assert body["model"] == _HF_MODEL  # the HF repo id
    assert body["tools"][0]["function"]["name"] == "http_fetch"


async def test_hf_router_response_parse() -> None:
    # text and tool-call responses from the HF router /v1 parse identically to
    # OpenAI's — same neutral ModelResponse the loop reads.
    text_p = _provider("hf_secret", _OPENAI_TEXT, [])
    r = await text_p.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))
    assert r.finish is Finish.STOP
    assert r.text == "hello world"
    assert r.usage["input_tokens"] == 10 and r.usage["total_tokens"] == 15

    tool_p = _provider("hf_secret", _OPENAI_TOOL, [])
    r2 = await tool_p.complete(
        ModelRequest(messages=[{"role": "user", "content": "fetch"}], tools=[_TOOL])
    )
    assert r2.finish is Finish.TOOL_CALLS
    assert r2.tool_calls[0].name == "http_fetch"
    assert r2.tool_calls[0].args == {"url": "http://e.test/"}  # parsed to a dict


async def test_hf_token_resolved_from_env_into_auth(monkeypatch) -> None:
    # No explicit api_key, api_key_env=HF_TOKEN with HF_TOKEN set: _ensure_client
    # builds a real AsyncOpenAI that carries the bearer derived from the env var.
    monkeypatch.setenv("HF_TOKEN", "hf_from_env")
    captured: list[httpx.Request] = []
    p = OpenAICompatibleProvider(model=_HF_MODEL, base_url=_HF_BASE, api_key_env="HF_TOKEN")
    # build the SDK client via _ensure_client, then swap in our capturing transport
    client = p._ensure_client()
    client._client = httpx.AsyncClient(transport=_capturing_transport(_OPENAI_TEXT, captured))
    await p.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))
    assert captured[0].headers["authorization"] == "Bearer hf_from_env"


@pytest.mark.parametrize(
    "base_url",
    [
        "https://router.huggingface.co/v1",
        "https://abc123.us-east-1.aws.endpoints.huggingface.cloud/v1",
        "http://localhost:8000/v1",
    ],
)
async def test_endpoint_and_local_vllm_are_same_adapter(base_url) -> None:
    # the one-adapter-three-surfaces claim, executable: the router, a dedicated
    # Inference Endpoint, and a local vLLM are the SAME provider + config; only
    # the base_url differs, and the path is always <base>/chat/completions.
    captured: list[httpx.Request] = []
    p = _provider("tok", _OPENAI_TEXT, captured, base_url=base_url)
    r = await p.complete(ModelRequest(messages=[{"role": "user", "content": "hi"}]))
    assert str(captured[0].url) == f"{base_url}/chat/completions"
    assert r.text == "hello world"  # parses identically on all three


async def test_hf_router_vlm_chat_image_passes_through() -> None:
    # A VLM policy: an image rides the SAME chat request (a multimodal content
    # list with an image_url data-URL part). to_openai_messages passes list
    # content straight through, so the image part reaches the wire intact.
    captured: list[httpx.Request] = []
    p = _provider("tok", _OPENAI_TEXT, captured)
    msg = {
        "role": "user",
        "content": [
            {"type": "text", "text": "what is this?"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ],
    }
    r = await p.complete(ModelRequest(messages=[msg]))
    body = json.loads(captured[0].content)
    content = body["messages"][0]["content"]
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["image_url"]["url"] == "data:image/png;base64,AAAA"  # image part intact
    assert r.text == "hello world"  # and the response still parses
