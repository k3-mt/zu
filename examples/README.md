# Examples

Runnable, copy-paste examples. (The end-to-end *proof suites* that build images
and assert containment live in [`../validation/`](../validation/), not here.)

## Agent examples — [`agents/`](agents/)

Real agents you can run with one command; each ships a fixture so the test suite
proves it works offline (no key, no network).

| Agent | Does | Shape |
|---|---|---|
| [`agents/price-extractor`](agents/price-extractor/) | fetch a product page → name + price, grounded | single run, tier 1 |
| [`agents/article-summary`](agents/article-summary/) | fetch an article → title + section headings (array), grounded | single run, tier 1 |
| [`agents/research-pipeline`](agents/research-pipeline/) | `extract → summarize` chained as one event-sourced run | **multi-phase** (`zu.Pipeline`) |

```bash
cd agents/price-extractor && zu run task.yaml -c zu.yaml      # real model (needs a key)
python agents/research-pipeline/pipeline.py                   # multi-phase, offline, no key
```

Scaffold your own starter pair (task.yaml + zu.yaml) with `zu init`. The full
build-an-agent guide — designing escalation, per-tier models, testing,
red-teaming, deploy — is in the published documentation.

## Integrations — [`integrations/`](integrations/)

Sample configs to drive Zu from a coding agent over MCP (Claude Code, Cursor,
Codex). Copy the one for your client; register `zu mcp` once.
