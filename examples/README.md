# Examples

Runnable demos.

## The killer demo

[`killer_demo.py`](killer_demo.py) — the whole arc in one run: an agent extracts
structured product data from a JS-heavy page that defeats a naive scraper. Watch
it fetch the page, **fail on JavaScript**, **escalate to a browser** (a decision
the harness makes via a detector, never the model), return the product, and have
that answer **validated against what the run actually fetched** — with the entire
run queryable afterward as an event log. One arc, all three pillars.

```bash
uv run python examples/killer_demo.py          # zero setup: fake model, fixtures
```

No API key, no network, no Docker — fully deterministic. Point it at a **real
model** to watch a live model make the same escalation decision (still no Docker;
the page is fixtured, so all you need is one key):

```bash
export ANTHROPIC_API_KEY=...
uv run python examples/killer_demo.py --provider anthropic --model claude-sonnet-4-6
```

## Also here

- [`zu.example.yaml`](zu.example.yaml) — a sample run config (the any-model
  seam: one `provider` block is the whole model swap). Wired by `zu run` —
  copy it to `zu.yaml`, fill a key, and `zu run task.yaml`.
- [`task.example.yaml`](task.example.yaml) — a sample task spec.
- [`scripted_demo.py`](scripted_demo.py) — a tiny, fully-offline tour of plugin
  discovery and the interpreter loop driving a fake model to a validated Result.
  Run it with `uv run python examples/scripted_demo.py`.
