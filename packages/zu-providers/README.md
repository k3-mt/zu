# zu-providers

Model adapters — the **`ModelProvider`** port (the any-model seam). An adapter
turns the harness's one normalized `ModelRequest` into a `ModelResponse` (text +
tool calls + usage + finish reason) and declares its `Capabilities`. The core
never special-cases a provider; it reads capabilities and proceeds.

**Credentials are resolved from the environment inside the adapter** — never
placed in the model's context or in a config file.

## Registered plugins (`zu.providers`)

| Name | Class | Notes |
|------|-------|-------|
| `scripted` | `ScriptedProvider` | The fake model: replays fixed moves in order. Deterministic; the basis of every offline test. No key, no network. |
| `anthropic` | `AnthropicProvider` | The Anthropic Messages API. Needs `[anthropic]` SDK extra + an API key. |
| `openai-compatible` | `OpenAICompatibleProvider` | Any OpenAI-compatible endpoint (OpenAI, OpenRouter, Ollama, vLLM) via a base URL. Needs `[openai]` SDK extra. |

`_messages.py` holds the shared request/response translation both real adapters
build on, so they behave identically against the neutral contract.

## Extend

Implement the `ModelProvider` shape, register it under `zu.providers` in
`pyproject.toml`, and add a deterministic test (the contract test asserts every
adapter behaves identically on the neutral surface).

## Tests

`uv run pytest packages/zu-providers` — offline. Live-API smoke tests are opt-in
behind `ZU_LIVE_*` env flags.
