# Examples

Runnable, copy-paste examples — purely examples. (The end-to-end *proof suites*
that build images and assert containment live in [`../validation/`](../validation/),
not here.)

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

See [`../docs/BUILD_AN_AGENT.md`](../docs/BUILD_AN_AGENT.md) for the full guide
(designing escalation, per-tier models, testing, red-teaming, deploy).

## Demos

- [`killer_demo.py`](killer_demo.py) — the whole arc in one run: fetch → **fail on
  JavaScript → escalate to a browser** → return a **validated** result, then print
  the queryable event log. Zero setup (fake model, fixtures); add
  `--provider anthropic --model …` (with a key) to watch a real model decide.
- [`scripted_demo.py`](scripted_demo.py) — a tiny offline tour of plugin discovery
  and the interpreter loop driving a fake model to a validated Result.

```bash
python killer_demo.py            # no key, no network, no Docker
```

## Starter configs

- [`task.example.yaml`](task.example.yaml) — a sample task (what you want).
- [`zu.example.yaml`](zu.example.yaml) — a sample run config (the one-line model
  swap). Copy to `zu.yaml`, set a key, `zu run task.yaml`. Or scaffold a pair
  with `zu init`.

## Integrations — [`integrations/`](integrations/)

Sample configs to drive Zu from a coding agent over MCP (Claude Code, Cursor,
Codex). Copy the one for your client; see QUICKSTART §9.
