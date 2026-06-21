# js-product

The tier-1 → tier-2 **escalation arc** as a runnable agent. A plain `http_fetch`
returns an empty JavaScript shell (`<div id="root">` + a script, no content); the
`js-shell` detector escalates; `render_dom` renders the page in a real browser and
the product **name** and **price** are extracted from the post-JS DOM, shaped to a
schema, and **grounded** in the rendered content.

```bash
export ANTHROPIC_API_KEY=sk-...
zu run agent.yaml          # real model, tier 2 (needs Docker for the browser)
```

```bash
zu run --offline           # replays fixtures/ — no key, no network, no Docker, ~$0
```

`--offline` is the cheap construction loop: it replays the captured `fixtures/`
bundle (`shell.html` for the tier-1 fetch, `rendered.html` for the tier-2 render,
`script.json` for the model's moves) so you can iterate the agent deterministically
and for free. The same `agent.yaml` runs live or offline. See
`tests/test_offline_run.py`.
