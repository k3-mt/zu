"""Anthropic Messages API adapter (build step 7).

Translates Zu's neutral ``ModelRequest`` into a Messages API call via the
official ``anthropic`` SDK, and the response back into a neutral
``ModelResponse`` — so the rest of the runtime never imports a model SDK. The
API key is resolved from the environment *inside* the adapter and never placed
in the model's context or in config, consistent with the security model.

The client is injectable (an ``AsyncAnthropic`` with a mock transport) so the
translation and parsing are proven offline against the real SDK; a live call is
opt-in. The same neutral contract is implemented by ``openai_compatible`` —
both pass one shared checklist, which is what makes "run on any model" real.
"""

from __future__ import annotations

import os
from typing import Any

from zu_core.ports import Capabilities, Finish, ModelRequest, ModelResponse, ToolCall

from ._messages import anthropic_tool, to_anthropic_messages

# Anthropic stop_reason -> neutral Finish. tool_use is handled by the presence
# of tool calls (set in complete) so a text+tool response still finalises right.
_FINISH = {
    "end_turn": Finish.STOP,
    "stop_sequence": Finish.STOP,
    "tool_use": Finish.TOOL_CALLS,
    "max_tokens": Finish.LENGTH,
    "refusal": Finish.STOP,
    "pause_turn": Finish.STOP,
}

# Default per-response output cap. Agent turns are short (a tool call or a small
# JSON answer); override per request via ``ModelRequest.params["max_tokens"]``.
_DEFAULT_MAX_TOKENS = 4096


class AnthropicProvider:
    def __init__(
        self,
        model: str = "claude-opus-4-8",
        api_key_env: str = "ANTHROPIC_API_KEY",
        api_key: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        client: Any = None,
    ) -> None:
        self.model = model
        self.api_key_env = api_key_env
        # An explicit key (for programmatic / in-memory use, e.g. zu.run with a
        # key your app already holds). Prefer ``api_key_env`` so the key never
        # lands in a committed config file; either way it stays out of the
        # model's context. Never hard-code or ship a key.
        self.api_key = api_key
        self.max_tokens = max_tokens
        # client is a testability/config seam (an AsyncAnthropic, possibly with a
        # mock transport); None -> construct from the resolved key on first use.
        self._client = client
        self.capabilities = Capabilities(native_tools=True, vision=True, max_context=1_000_000)

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                import anthropic
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "the anthropic provider needs the SDK: "
                    "pip install 'zu-runtime[anthropic]'"
                ) from exc

            key = self.api_key or os.environ.get(self.api_key_env)
            if not key:
                raise RuntimeError(
                    f"no Anthropic API key: pass api_key=... or set ${self.api_key_env} "
                    "(the key is read here, never placed in the model's context or a config file)."
                )
            self._client = anthropic.AsyncAnthropic(api_key=key)
        return self._client

    async def complete(self, req: ModelRequest) -> ModelResponse:
        client = self._ensure_client()
        system, messages = to_anthropic_messages(req.messages)
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": int(req.params.get("max_tokens", self.max_tokens)),
            "messages": messages,
        }
        if system:
            kwargs["system"] = system
        if req.tools:
            kwargs["tools"] = [anthropic_tool(t) for t in req.tools]
        resp = await client.messages.create(**kwargs)
        return _to_model_response(resp)


def _to_model_response(resp: Any) -> ModelResponse:
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in resp.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            calls.append(ToolCall(name=block.name, args=dict(block.input or {})))
    finish = Finish.TOOL_CALLS if calls else _FINISH.get(resp.stop_reason, Finish.STOP)
    usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens}
    return ModelResponse(
        text="".join(text_parts) or None,
        tool_calls=calls,
        finish=finish,
        usage=usage,
    )
