# price-extractor

The five-minute promise as a runnable agent: fetch a product page, extract its
**name** and **price**, shape the result to a schema, and **ground** every value
in the fetched content (a fabricated price is refused, not returned).

```bash
export ANTHROPIC_API_KEY=sk-...
zu run task.yaml -c zu.yaml          # real model, tier 1 (network, no Docker)
```

Tier 1 only — needs network for `http_fetch`, no Docker. The repo's test suite
runs this agent **offline** against `fixtures/product.html` (the real tools +
validators, a scripted model), so the wiring and the schema/grounding contract
are proven with no key and no network. See `tests/test_example_agents.py`.
