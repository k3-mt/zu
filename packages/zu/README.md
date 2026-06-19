# Zu

**An opinionated, backend-agnostic runtime for agents that work in production** —
deterministic, auditable, and injection-resistant by construction.

`pip install zu-runtime` gives you `import zu`, the `zu` command, and every
built-in plugin (model providers, tools, detectors, validators, a sandbox
backend, and a SQLite event sink) discovered out of the box.

```bash
pip install zu-runtime          # the library + CLI + built-ins
pip install 'zu-runtime[all]'   # + HTTP server, Anthropic/OpenAI SDKs, Docker sandbox
```

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
zu run task.yaml -c zu.yaml             # one-shot
zu run task.yaml -c zu.yaml --every 5m  # scheduled worker
zu serve -c zu.yaml                     # HTTP: POST /run  (needs the [serve] extra)
```

## What it is

A small, stable core (the loop, registry, contracts, event bus) surrounded by
six swappable ports. Every capability that can vary is a plugin behind a port,
so the production system is reached by adding adapters — never by reopening the
core. Full source, architecture, and examples:
**https://github.com/k3-mt/zu**

Apache-2.0.
