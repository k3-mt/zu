# Zu

**An opinionated, backend-agnostic runtime for agents that work in production** —
deterministic, auditable, and injection-resistant by construction.

A lean base, plugins opt-in (dbt-style). `pip install zu-runtime` gives you
`import zu`, the `zu` command, the model-provider adapters, detectors,
validators, and a SQLite event sink. Add what you need:

```bash
pip install zu-runtime              # runnable base
pip install 'zu-runtime[web]'       # + web tools (http_fetch, html_parse, render_dom)
pip install 'zu-runtime[anthropic]' # + Anthropic SDK     (also: [openai])
pip install 'zu-runtime[serve]'     # + HTTP server (zu serve)
pip install 'zu-runtime[all]'       # everything (web + both SDKs + server + Docker)
```

Each plugin is also a standalone package (`pip install zu-tools`, …), the way
dbt ships adapters.

## Embed it

```python
import zu

result = zu.run(
    {"query": "Extract the product name and price.",
     "target": "https://example.com/product/123",
     "output_schema": {"type": "object",
                       "properties": {"name": {"type": "string"}, "price": {"type": "string"}},
                       "required": ["name", "price"]}},
    config={"provider": {"name": "anthropic", "model": "claude-sonnet-4-6",
                         "api_key_env": "ANTHROPIC_API_KEY"},
            "plugins": {"tools": ["http_fetch", "html_parse", "render_dom"],
                        "detectors": ["empty", "error", "js-shell", "bot-wall"],
                        "validators": ["schema", "grounding"]}},
)
print(result.status, result.value)
```

Swapping the model is a one-line edit to the `provider` block — Anthropic,
OpenRouter, OpenAI, or a local model (Ollama / vLLM) — because the runtime only
ever speaks to a `ModelProvider` port. Credentials are named by environment
variable (`api_key_env`), never passed in code or config.

## Run it from the command line, or as a service

```bash
zu run agent.yaml             # one-shot
zu run agent.yaml --every 5m  # scheduled worker
zu serve -c agent.yaml                     # HTTP: POST /run  (needs the [serve] extra)
```

## What it is

A small, stable core (the loop, registry, contracts, event bus) surrounded by
six swappable ports. Every capability that can vary is a plugin behind a port,
so the production system is reached by adding adapters — never by reopening the
core. Full source, architecture, and examples:
**https://github.com/k3-mt/zu**

Apache-2.0.
