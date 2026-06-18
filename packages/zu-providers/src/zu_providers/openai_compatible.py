"""OpenAI-compatible adapter (build step 7).

One adapter, pointed at a different base URL, reaches OpenRouter, OpenAI, and
local servers (Ollama, vLLM) — covering a vast range of models, including open
ones. If a model lacks native tool-calling, the adapter transparently falls
back to a prompt-based tool protocol (inject schemas into the prompt, parse a
structured action out of the text) so the same harness still works.

Importable now for discovery; wired in build step 7.
"""

from __future__ import annotations

from zu_core.ports import Capabilities, ModelRequest, ModelResponse


class OpenAICompatibleProvider:
    def __init__(
        self,
        model: str,
        base_url_env: str = "OPENAI_BASE_URL",
        api_key_env: str = "OPENAI_API_KEY",
        native_tools: bool = True,
    ) -> None:
        self.model = model
        self.base_url_env = base_url_env
        self.api_key_env = api_key_env
        self.capabilities = Capabilities(native_tools=native_tools)

    async def complete(self, req: ModelRequest) -> ModelResponse:
        raise NotImplementedError(
            "OpenAICompatibleProvider is build step 7. Translate ModelRequest -> "
            "the Chat Completions schema; when capabilities.native_tools is False, "
            "use the prompt-based tool fallback. Base URL and key come from the "
            "named environment variables."
        )
