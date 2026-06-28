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

## Discovery & trust — the open-web research siblings (#81, #84)

Two ports for an agent that must **find** and **vet** a vendor on the open web,
not just act on a named site. Both return typed records, never page prose, so the
content-free discipline holds through discovery.

### `RetrievalProvider` — typed discovery (`zu.retrieval_providers`, #81)

`search(RetrievalQuery) -> list[Candidate]`: turn a spec into typed candidate
records (title, url, domain, price in minor units, in_stock, source) the agent
ranks over a schema.

| Name | Class | Notes |
|------|-------|-------|
| `scripted` | `ScriptedRetrievalProvider` | The fake: replays canned candidates. Deterministic, no key, no network — the discovery analog of `ScriptedProvider`. |
| `web_search` | `WebSearchRetrievalProvider` | The open-web fallback: reduces the `web_search` tool to typed candidates, mirroring its scoped egress. Carries only what search yields (title/url/domain). |

### `ReputationProvider` — computed merchant trust (`zu.reputation_providers`, #84)

`assess(domain) -> ReputationVerdict` (`band` trusted/caution/refuse, `score`,
`gate`, auditable `signals` + `provenance`): a deterministic decision over
hard-to-forge domain signals — never page content, so it is injection-immune.

| Name | Class | Notes |
|------|-------|-------|
| `deterministic` | `DeterministicReputationScorer` | Forge-resistance-weighted scoring with hard gates (blocklist / no-HTTPS / parked → refuse) and two axes (is-it-malicious vs. is-it-a-real-shop). Pure scoring over a pluggable `SignalSource` seam (`StaticSignalSource` for $0 tests; network fetchers drop in behind the same shape). |

## Extend

Implement the `ModelProvider` shape, register it under `zu.providers` in
`pyproject.toml`, and add a deterministic test (the contract test asserts every
adapter behaves identically on the neutral surface).

## Tests

`uv run pytest packages/zu-providers` — offline. Live-API smoke tests are opt-in
behind `ZU_LIVE_*` env flags.
