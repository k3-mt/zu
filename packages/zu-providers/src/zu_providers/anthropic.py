"""Anthropic Messages API adapter (build step 7).

Importable now so the registry can discover it; the live call is wired in
step 7, after the loop, tools, detectors, and validation are proven against
the ScriptedProvider. The API key is resolved from the environment *inside*
the adapter and never placed in the model's context — consistent with the
capability-envelope security model.
"""

from __future__ import annotations

from zu_core.ports import Capabilities, ModelRequest, ModelResponse


class AnthropicProvider:
    def __init__(
        self,
        model: str = "claude-sonnet-4-6",
        api_key_env: str = "ANTHROPIC_API_KEY",
    ) -> None:
        self.model = model
        self.api_key_env = api_key_env
        self.capabilities = Capabilities(native_tools=True, vision=True, max_context=200_000)

    async def complete(self, req: ModelRequest) -> ModelResponse:
        raise NotImplementedError(
            "AnthropicProvider is build step 7. Translate ModelRequest -> the "
            "Messages API, parse tool_use blocks into ToolCall, and resolve the "
            "key from os.environ[self.api_key_env]."
        )
