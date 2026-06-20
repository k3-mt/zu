# article-summary

Fetch an article and extract its **title** and the text of each **section
heading** — a non-scalar (array) output shape, held to the same schema +
grounding contract: every heading must actually appear on the page.

```bash
export ANTHROPIC_API_KEY=sk-...
zu run agent.yaml          # real model, tier 1
```

Runs offline in the test suite against `fixtures/article.html` (real tools +
validators, scripted model). See `tests/test_example_agents.py`.
