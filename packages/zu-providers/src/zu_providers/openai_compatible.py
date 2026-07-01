"""OpenAI-compatible adapter (build step 7).

One adapter, pointed at a different base URL, reaches OpenRouter, OpenAI, and
local servers (Ollama, vLLM) — covering a vast range of models, including open
ones. It translates Zu's neutral ``ModelRequest`` into a Chat Completions call
via the official ``openai`` SDK and parses the response back, so the rest of
the runtime never imports a model SDK. Base URL and key are resolved from the
environment *inside* the adapter, never placed in the model's context.

The client is injectable (an ``AsyncOpenAI`` with a mock transport) so the
translation and parsing are proven offline against the real SDK; a live call is
opt-in. This adapter and ``anthropic`` pass one shared checklist — identical
neutral behaviour from two different wire formats.

A model without native tool-calling would need the prompt-based tool fallback
(inject schemas into the prompt, parse a structured action out of the text);
that path is deferred. The native path is what ships here.
"""

from __future__ import annotations

import json
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

from ._messages import openai_tool, to_openai_messages

logger = logging.getLogger("zu.providers.openai")

# OpenAI finish_reason -> neutral Finish. A tool-call finish is decided by the
# presence of tool calls, not this map, so the tool_calls/function_call reasons
# are intentionally absent here.
_FINISH = {
    "stop": Finish.STOP,
    "length": Finish.LENGTH,
    "content_filter": Finish.STOP,
}

# Default per-call wall-time and retry bounds — see the anthropic adapter: a
# production runtime must not inherit the SDK's unbounded timeout / stacked
# backoff. Override per provider via the constructor (or config ``options``).
_DEFAULT_TIMEOUT_S = 60.0
_DEFAULT_MAX_RETRIES = 2


class OpenAICompatibleProvider:
    def __init__(
        self,
        model: str,
        base_url_env: str = "OPENAI_BASE_URL",
        api_key_env: str = "OPENAI_API_KEY",
        api_key: str | None = None,
        base_url: str | None = None,
        native_tools: bool = True,
        max_tokens: int | None = None,
        timeout: float = _DEFAULT_TIMEOUT_S,
        max_retries: int = _DEFAULT_MAX_RETRIES,
        client: Any = None,
    ) -> None:
        self.model = model
        self.base_url_env = base_url_env
        self.api_key_env = api_key_env
        # Explicit key/base_url for programmatic use; prefer the *_env forms so a
        # key never lands in a committed config. Either way it stays out of the
        # model's context. Never hard-code or ship a key.
        self.api_key = api_key
        self.base_url = base_url
        self.max_tokens = max_tokens
        self.timeout = timeout
        self.max_retries = max_retries
        self._client = client
        self.capabilities = Capabilities(native_tools=native_tools)

    def _ensure_client(self) -> Any:
        if self._client is None:
            try:
                import openai
            except ModuleNotFoundError as exc:
                raise RuntimeError(
                    "the openai-compatible provider needs the SDK: "
                    "pip install 'zu-runtime[openai]'"
                ) from exc

            # Local servers (Ollama/vLLM) need no key; the SDK still wants a
            # non-empty string, so fall back to a placeholder. Base URL is
            # optional (defaults to OpenAI) and read from the env when set.
            key = self.api_key or os.environ.get(self.api_key_env) or "not-needed"
            base_url = self.base_url or os.environ.get(self.base_url_env) or None
            self._client = openai.AsyncOpenAI(
                api_key=key,
                base_url=base_url,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
        return self._client

    async def complete(self, req: ModelRequest) -> ModelResponse:
        if not self.capabilities.native_tools:
            raise NotImplementedError(
                "prompt-based tool fallback for non-native-tool models is deferred; "
                "set native_tools=True to use the Chat Completions path."
            )
        client = self._ensure_client()
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": to_openai_messages(req.messages),
        }
        if req.tools:
            kwargs["tools"] = [openai_tool(t) for t in req.tools]
        max_tokens = req.params.get("max_tokens", self.max_tokens)
        if max_tokens is not None:
            kwargs["max_tokens"] = int(max_tokens)
        try:
            resp = await client.chat.completions.create(**kwargs)
        except Exception as exc:  # translate SDK errors -> neutral port surface
            raise _translate_error(exc) from exc
        return _to_model_response(resp)


def _translate_error(exc: Exception) -> ProviderError:
    """Map an ``openai`` SDK exception to the neutral provider-error taxonomy.

    The SDK class names appear in exactly this one place per package, so the rest
    of the runtime imports no model SDK on the error path either. Order matters:
    the most specific classes are checked first (a ``RateLimitError`` IS an
    ``APIStatusError``). An unrecognised exception wraps in the base
    ``ProviderError`` so nothing vendor-specific escapes the port. The mapping is
    the mirror of the anthropic adapter's, so both ports present one surface."""
    try:
        import openai
    except ModuleNotFoundError:  # pragma: no cover - SDK present whenever a call ran
        return ProviderError(str(exc))
    msg = str(exc)
    if isinstance(exc, openai.AuthenticationError | openai.PermissionDeniedError):
        return ProviderAuthError(msg)
    if isinstance(exc, openai.RateLimitError):
        return ProviderRateLimited(msg)
    if isinstance(exc, openai.APITimeoutError):
        return ProviderTimeout(msg)
    if isinstance(exc, openai.APIConnectionError | openai.InternalServerError):
        return ProviderUnavailable(msg)
    if isinstance(exc, openai.OpenAIError):
        return ProviderError(msg)
    return ProviderError(msg)


def _to_model_response(resp: Any) -> ModelResponse:
    # Some OpenAI-compatible servers (vLLM/Ollama/proxies) return an empty
    # ``choices`` array on certain errors or policy stops. Index [0] would
    # IndexError; instead surface it as a no-answer STOP (the loop ends the run
    # cleanly with "model finalised with no answer") and keep any usage reported.
    choices = resp.choices or []
    if not choices:
        logger.warning("provider returned no choices (mapped to an empty STOP response)")
        return ModelResponse(text=None, tool_calls=[], finish=Finish.STOP, usage=_usage_of(resp))
    choice = choices[0]
    msg = choice.message
    calls: list[ToolCall] = []
    for tc in msg.tool_calls or []:
        raw = tc.function.arguments or "{}"
        try:
            args = json.loads(raw)
        except (ValueError, TypeError):
            args = {}
        if not isinstance(args, dict):
            args = {}
        if args == {} and raw not in ("", "{}"):
            # Malformed (or non-object) tool args. We keep the run alive — a
            # malformed-args tool call becomes an empty-args call the loop will
            # still dispatch — but we do NOT swallow it: surface it as a warning
            # so a model emitting broken arguments is visible, not silent.
            logger.warning(
                "tool call %r produced unparsable arguments, dispatching with {}: %r",
                tc.function.name,
                raw,
            )
        calls.append(ToolCall(name=tc.function.name, args=args))
    if not calls and choice.finish_reason == "content_filter":
        # The provider's moderation stopped generation. The neutral Finish set has
        # no distinct moderation value, so it maps to STOP — but we do NOT collapse
        # it silently: surface it so a refusal/cut-off is visible, not mistaken for
        # a clean completion (the same "fail loudly" posture as malformed args).
        logger.warning("model response stopped by content_filter (mapped to STOP)")
    finish = Finish.TOOL_CALLS if calls else _FINISH.get(choice.finish_reason, Finish.STOP)
    return ModelResponse(
        text=msg.content or None, tool_calls=calls, finish=finish, usage=_usage_of(resp)
    )


def _usage_of(resp: Any) -> dict:
    """The normalised usage shape (input/output/total) shared with the anthropic
    adapter, degrading to ``{}`` when the provider reports no usage."""
    raw = getattr(resp, "usage", None)
    if raw is None:
        return {}
    return {
        "input_tokens": raw.prompt_tokens,
        "output_tokens": raw.completion_tokens,
        "total_tokens": raw.total_tokens,
    }
