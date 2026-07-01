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

import logging
import os
from typing import Any

from zu_core.errors import (
    ProviderAuthError,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from zu_core.ports import Capabilities, Finish, ModelRequest, ModelResponse, ToolCall

from ._messages import anthropic_tool, to_anthropic_messages

logger = logging.getLogger("zu.providers.anthropic")

# Anthropic stop_reason -> neutral Finish. A tool-call finish is decided by the
# presence of tool calls, not this map, so the ``tool_use`` reason is absent here
# (a text+tool response still finalises right via presence-of-calls).
_FINISH = {
    "end_turn": Finish.STOP,
    "stop_sequence": Finish.STOP,
    "max_tokens": Finish.LENGTH,
    "refusal": Finish.STOP,
    "pause_turn": Finish.STOP,
}

# Default per-response output cap. Agent turns are short (a tool call or a small
# JSON answer); override per request via ``ModelRequest.params["max_tokens"]``.
_DEFAULT_MAX_TOKENS = 4096

# Default per-call wall-time and retry bounds. A "production runtime" must not
# inherit the SDK's unbounded defaults: a hung connection or the SDK's own
# exponential-backoff retries can otherwise stack arbitrarily inside a short
# run budget. The loop wraps ``complete`` in its own wall-time too, but the
# adapter sets a floor so direct/embed use (no loop deadline) is bounded as well.
_DEFAULT_TIMEOUT_S = 60.0
_DEFAULT_MAX_RETRIES = 2


class AnthropicProvider:
    def __init__(
        self,
        model: str = "claude-opus-4-8",
        api_key_env: str = "ANTHROPIC_API_KEY",
        api_key: str | None = None,
        max_tokens: int = _DEFAULT_MAX_TOKENS,
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = _DEFAULT_MAX_RETRIES,
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
        self.timeout = timeout
        self.max_retries = max_retries
        # client is a testability/config seam (an AsyncAnthropic, possibly with a
        # mock transport); None -> construct from the resolved key on first use.
        self._client = client
        # vision=True: the neutral request carries image blocks (LlmPolicy builds
        # them) and this adapter translates them to Anthropic's image/source wire
        # shape in to_anthropic_messages, so the model genuinely receives images.
        # LlmPolicy gates image content on this flag, so it must reflect reality.
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
            self._client = anthropic.AsyncAnthropic(
                api_key=key, timeout=self.timeout, max_retries=self.max_retries
            )
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
        try:
            resp = await client.messages.create(**kwargs)
        except Exception as exc:  # translate SDK errors -> neutral port surface
            raise _translate_error(exc) from exc
        return _to_model_response(resp)


def _translate_error(exc: Exception) -> ProviderError:
    """Map an ``anthropic`` SDK exception to the neutral provider-error taxonomy.

    The SDK class names appear in exactly this one place per package, so the rest
    of the runtime imports no model SDK on the error path either. Order matters:
    the most specific classes are checked first (a ``RateLimitError`` IS an
    ``APIStatusError``). An unrecognised exception wraps in the base
    ``ProviderError`` so nothing vendor-specific escapes the port."""
    try:
        import anthropic
    except ModuleNotFoundError:  # pragma: no cover - SDK present whenever a call ran
        return ProviderError(str(exc))
    msg = str(exc)
    if isinstance(exc, anthropic.AuthenticationError | anthropic.PermissionDeniedError):
        return ProviderAuthError(msg)
    if isinstance(exc, anthropic.RateLimitError):
        return ProviderRateLimited(msg)
    if isinstance(exc, anthropic.APITimeoutError):
        return ProviderTimeout(msg)
    if isinstance(exc, anthropic.APIConnectionError | anthropic.InternalServerError):
        return ProviderUnavailable(msg)
    if isinstance(exc, anthropic.AnthropicError):
        return ProviderError(msg)
    return ProviderError(msg)


def _to_model_response(resp: Any) -> ModelResponse:
    text_parts: list[str] = []
    calls: list[ToolCall] = []
    for block in resp.content:
        if block.type == "text":
            text_parts.append(block.text)
        elif block.type == "tool_use":
            calls.append(ToolCall(name=block.name, args=dict(block.input or {})))
    if not calls and resp.stop_reason == "refusal":
        # A model refusal. No distinct neutral Finish exists, so it maps to STOP —
        # but warn rather than collapse it silently, so a refusal isn't mistaken
        # for a clean completion (mirrors the openai adapter's content_filter).
        logger.warning("model response was a refusal (mapped to STOP)")
    finish = Finish.TOOL_CALLS if calls else _FINISH.get(resp.stop_reason, Finish.STOP)
    # Normalised usage shape shared with the openai-compatible adapter:
    # input/output/total. Anthropic's API doesn't return a total, so compute it
    # (input + output) — both adapters hand the cost projection the same shape.
    # Guard a missing/partial usage object the same way the openai adapter does,
    # so a response without usage degrades to {} rather than raising AttributeError
    # — the two adapters behave identically on this edge, not just the happy path.
    raw_usage = getattr(resp, "usage", None)
    if raw_usage is None:
        usage: dict = {}
    else:
        in_tok = getattr(raw_usage, "input_tokens", 0) or 0
        out_tok = getattr(raw_usage, "output_tokens", 0) or 0
        usage = {"input_tokens": in_tok, "output_tokens": out_tok, "total_tokens": in_tok + out_tok}
    return ModelResponse(
        text="".join(text_parts) or None,
        tool_calls=calls,
        finish=finish,
        usage=usage,
    )
