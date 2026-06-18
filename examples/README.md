# Examples

Runnable demos. The **killer demo** lives here once the loop lands (build steps
4–9): extract structured product data from a JS-heavy site that defeats a naive
scraper — watch the agent fetch the page, fail on JavaScript, **escalate to a
browser**, and return validated structured data, with the event log queryable
afterward. That single arc demonstrates all three pillars in one run.

## What's here today

- [`zu.example.yaml`](zu.example.yaml) — a sample run config (two tiers, the
  any-model seam). Wired by `zu run` at build step 8.
- [`task.example.yaml`](task.example.yaml) — a sample task spec.
- [`scripted_demo.py`](scripted_demo.py) — a tiny, fully-offline script showing
  the plugin registry and the `ScriptedProvider` (the fake model) today, before
  the loop exists. Run it with `uv run python examples/scripted_demo.py`.
