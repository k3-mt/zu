# Example agents

Small, real, runnable agents — each a single `agent.yaml` (the whole agent: task +
model + the tier ladder of tools) you run with one command, plus a saved fixture
page so the repo's test suite proves the agent works **offline** (real tools +
validators, a scripted model — no key, no network).

| Agent | Does | Tier |
|---|---|---|
| [`price-extractor`](price-extractor/) | fetch a product page → name + price, grounded | 1 |
| [`article-summary`](article-summary/) | fetch an article → title + section headings (array), grounded | 1–2 |

```bash
export ANTHROPIC_API_KEY=sk-...
cd price-extractor && zu run agent.yaml      # real model
```

How they're tested (the test tiers):

- **unit lane** — `tests/test_example_agents.py` runs each agent offline against
  its fixture (the real `http_fetch`/`html_parse` + `schema`/`grounding`, a
  scripted model) and asserts the shipped `agent.yaml` parses and resolves.
- **docker lane** — `validation/containment/` runs the whole agent **inside the
  hardened container** behind the egress proxy, surfacing the in-container event
  log across the boundary.
