# Quickstart — build, run, and deploy an agent

This is the builder's guide: install Zu, define an agent with a task + config,
then run it four ways — embedded in your code, from the CLI, as an HTTP service,
and in a container — plus how to schedule it. Every step works offline first
(with the fake model), so you can see the whole shape before spending a token.

> **Concepts in one line.** A **task** is *what you want* (a query, a target, and
> the JSON shape of the answer). A **config** is *how to run it* (which model,
> which plugins, where to store the event log). The runtime drives a tool-using
> loop to a validated `Result`, recording every step as an event.

---

## Prerequisites

| Requirement | When you need it |
|---|---|
| **Python 3.11+** | Always — the only hard requirement. |
| **An API key** | Only to run against a **real model** (Anthropic / OpenAI / OpenRouter). Offline/scripted runs and both demos' default mode need no key. A local model (Ollama / vLLM) needs no key either. |
| **Docker** | **Not required** for the base, embedding, `zu serve`, the tier-1 web tools (`http_fetch`, `html_parse`), or the demos (they fixture the browser). It is required **only** when a real run actually escalates to the **tier-2 browser** — `render_dom` via the default `local-docker` backend launches a headless-Chromium container, so that needs the Docker daemon running plus `pip install 'zu-runtime[docker]'`. |

**So: you do _not_ need Docker to install Zu, run the demos, embed it, serve it,
or run tier-1 web extraction.** You need it only to render JavaScript pages in a
real browser (tier 2). Until then, everything runs with just Python.

### The tier-2 browser image

`render_dom` runs a headless Chromium inside a container via the `local-docker`
backend. That needs the Docker daemon and `pip install 'zu-runtime[docker]'`. The
render image is published at `ghcr.io/k3-mt/zu-render-chromium:latest` and is
`render_dom`'s default, so it's pulled automatically the first time you escalate.
To customise it, rebuild from this repo and point `render_dom` at your tag:

```bash
docker build -t my/zu-render:latest images/render-chromium
# RenderDom(image="my/zu-render:latest")  — or set it in config
```

The image stays running and exposes a `zu-render <url>` entrypoint that prints
`{"status","html","url"}` — render any URL in a real browser (JS executed). This
is the only part of Zu that needs Docker.

---

## 1. Install

A lean base, plugins opt-in (the same shape as dbt: a small core, adapters you
add). The base gives you `import zu`, the `zu` command, the model-provider
adapters, detectors, validators, and the sqlite event sink:

```bash
pip install zu-runtime            # the runnable base (above)
```

Add what your agent needs:

| Install | Adds |
|---|---|
| `pip install 'zu-runtime[web]'` | web tools — `http_fetch`, `html_parse`, `render_dom` (the browser tier) |
| `pip install 'zu-runtime[anthropic]'` | the Anthropic SDK (the adapter ships in the base) |
| `pip install 'zu-runtime[openai]'` | the OpenAI SDK (covers OpenAI / OpenRouter / Ollama / vLLM) |
| `pip install 'zu-runtime[serve]'` | the HTTP server (`zu serve`) |
| `pip install 'zu-runtime[docker]'` | the Docker sandbox client (tier-2 browser) |
| `pip install 'zu-runtime[all]'` | everything above |

Every plugin is also a standalone package — `pip install zu-tools`,
`zu-providers`, `zu-detectors`, `zu-validators`, `zu-backends` — exactly the way
dbt ships adapters. Mix and match; `zu plugins` lists whatever is installed.

Verify and see every plugin the runtime discovered:

```bash
zu plugins
```

### Try it for real

The demo runs against a **real model** — the point is to prove Zu actually runs,
not just that the logic is wired. So it needs an API key. Pick a demo with
`--type` (each needs a bit more than the last):

```bash
export ANTHROPIC_API_KEY=sk-...

# minimal — a model answers a question as JSON, schema-validated. No tools, no
# network. Needs: an API key (and the model SDK: pip install 'zu-runtime[anthropic]').
zu demo --type minimal --model claude-sonnet-4-6

# web (default) — a real http_fetch of a real page + the model extracts a field +
# validation. This is TIER 1: needs an API key + network, the [demo] extra — NO Docker.
pip install 'zu-runtime[demo,anthropic]'
zu demo --model claude-sonnet-4-6
```

Other providers work the same way (`--provider openai-compatible --model … --base-url-env …`).

**Self-test without a key** — replays a scripted, fixtured run to prove the
*wiring* (handy for CI); it is labelled as not-a-real-run:

```bash
zu demo --offline                 # web wiring (fixtured fetch)
zu demo --offline --type escalation   # the escalation logic (fixtured browser)
```

> **`escalation` (tier 2)** is the full fetch → fail-on-JS → escalate-to-browser
> arc. The real path needs **Docker** *and* a published headless-Chromium image,
> which isn't available yet — so today escalation is `--offline` only. A real
> `web` (tier-1) run is the end-to-end proof you can do today with just a key.

```bash
export ANTHROPIC_API_KEY=sk-...
zu demo --provider anthropic --model claude-sonnet-4-6
# or pass the key directly, without an env var:
zu demo --provider anthropic --model claude-sonnet-4-6 --api-key sk-...
```

Real providers need their SDK — `pip install 'zu-runtime[anthropic]'` (or
`[openai]`). No key is ever bundled with the package; you always supply your own.

## 2. Define the agent: a config and a task

**`zu.yaml`** — how to run (the model is a one-line swap):

```yaml
provider:
  name: anthropic                 # scripted | anthropic | openai-compatible | <module:Class>
  model: claude-sonnet-4-6
  api_key_env: ANTHROPIC_API_KEY  # the env var NAME — never the key itself
plugins:
  tools: [http_fetch, html_parse, render_dom]
  detectors: [empty, error, js-shell, bot-wall]
  validators: [schema, grounding]
event_sink: { driver: sqlite, path: ./zu.db }
budget: { max_steps: 20, max_tokens: 200000, wall_time_s: 120 }
```

Swap to OpenRouter or a local model by editing only the `provider` block:

```yaml
provider:
  name: openai-compatible
  model: "anthropic/claude-3.5-haiku"   # any model the endpoint serves
  base_url_env: OPENROUTER_BASE_URL     # or OPENAI_BASE_URL=http://localhost:11434/v1 for Ollama
  api_key_env: OPENROUTER_API_KEY
```

**`task.yaml`** — what you want:

```yaml
query: "Extract the product name and price."
target: "https://example.com/product/123"
output_schema:
  type: object
  properties:
    name: { type: string }
    price: { type: string }
  required: [name, price]
```

Put your keys in the environment (never in the files):

```bash
export ANTHROPIC_API_KEY=sk-...
```

## 3. Run it — from the CLI

```bash
zu run task.yaml -c zu.yaml
```

A **live trace streams as the loop runs** — the model's train of thought, every
tool call and result, detector verdicts, and escalations — so the run is never a
black box:

```
  09:20:25  ▶ task: Extract the product name and price. → https://…
  09:20:25  💭 I'll fetch the page first, then read the heading.
  09:20:25  🔧 http_fetch({'url': 'https://…'})
  09:20:25  📄 fetched 1024 chars (status 200)
  09:20:26  🔎 detector js-shell [escalate] — page appears to be a JS shell
  09:20:26  ⬆️  ESCALATE 1→2: js-shell — climbing a tier
  09:20:28  📦 extracted: {'name': 'Acme Widget', 'price': '$9.00'}
  09:20:28  ✅ completed
```

Disable it with `--no-stream` (e.g. in CI). The status, value, and event count
print at the end; a non-success run exits non-zero, so it composes in a shell.

## 4. Embed it — in your code

```python
import zu

# from files…
result = zu.run("task.yaml", config="zu.yaml")

# …or from plain dicts, no files needed
result = zu.run(
    {"query": "Extract the product name and price.",
     "target": "https://example.com/product/123",
     "output_schema": {"type": "object",
                       "properties": {"name": {"type": "string"}, "price": {"type": "string"}},
                       "required": ["name", "price"]}},
    config="zu.yaml",
)

print(result.status, result.value)

# also want the event log (the queryable provenance)?
result, events = zu.run_with_events("task.yaml", config="zu.yaml")

# load a config once, run many tasks; async variants exist too (arun / arun_with_events)
agent = zu.Zu(config="zu.yaml")
r = agent.run({"query": "..."})
```

Passing a key your app already holds (in-memory, never written to a file): put
`api_key` in the provider block of a **config dict** (not a committed YAML):

```python
result = zu.run(task, config={
    "provider": {"name": "anthropic", "model": "claude-sonnet-4-6", "api_key": my_key},
    "plugins": {"validators": ["schema"]},
})
```

Prefer `api_key_env` (naming the env var) in files so a secret is never
committed; use the direct `api_key` only for in-memory configs.

## 5. Serve it — as an HTTP service

```bash
pip install 'zu-runtime[serve]'
zu serve -c zu.yaml --host 0.0.0.0 --port 8000
```

```bash
curl -s localhost:8000/run \
  -H 'content-type: application/json' \
  -d '{"task": {"query": "Extract the title.", "target": "https://example.com",
                "output_schema": {"type":"object","properties":{"title":{"type":"string"}}}}}'
```

The response is `{"result": {...}, "events": [...]}`. A request may include a
`config` object to override the server default per call, and `include_events:
false` to omit the log. `GET /healthz` is the liveness probe.

**Watch a run live over HTTP** — `POST /run/stream` streams Server-Sent Events as
the loop runs (one `event` frame per step, then `result`, then `done`). No
polling, no refresh; works the same against a local process or a container:

```bash
curl -N localhost:8000/run/stream \
  -H 'content-type: application/json' \
  -d '{"task": {"query": "Extract the title.", "target": "https://example.com",
                "output_schema": {"type":"object","properties":{"title":{"type":"string"}}}}}'
```

Each frame carries both a human-readable `line` and the full structured `event`,
so a browser `EventSource` or a dashboard can render the train of thought live.

Mounting in your own ASGI app instead of running `zu serve`:

```python
from zu import create_app
app = create_app("zu.yaml")   # a FastAPI/ASGI app
```

## 6. Containerize it

```bash
docker build -t zu .
docker run -p 8000:8000 -v "$PWD/zu.yaml:/app/zu.yaml" -e ANTHROPIC_API_KEY zu
```

The image serves on `:8000` by default. Override the command for a one-shot or
scheduled run: `docker run ... zu run task.yaml -c zu.yaml --every 5m`. Secrets
are passed with `-e` (read by the adapter at call time), never baked into the
image.

## 7. Schedule it

Built-in interval worker (good inside a container or a process supervisor):

```bash
zu run task.yaml -c zu.yaml --every 5m            # forever, every 5 minutes
zu run task.yaml -c zu.yaml --every 1h --max-runs 24
```

Or drive it from cron / a cloud scheduler — each tick is a one-shot `zu run`:

```cron
*/15 * * * *  cd /srv/agent && /usr/local/bin/zu run task.yaml -c zu.yaml >> run.log 2>&1
```

## 8. Drive it from your coding agent (MCP)

Live in Claude Code / Cursor / Codex and let the agent design, validate, run, and
inspect Zu agents for you — in natural language. One stdio server works across
all of them:

```bash
pip install 'zu-runtime[mcp]'      # adds the `zu mcp` server
```

Register it once (the harness then launches `zu mcp` as a child process per
session — stdio, no port, no daemon, killed on exit; nothing runs until asked):

```bash
# Claude Code
claude mcp add --transport stdio zu -- zu mcp
# Cursor   → copy examples/integrations/cursor.mcp.json to .cursor/mcp.json
# Codex    → append examples/integrations/codex-config.toml to ~/.codex/config.toml
```

Then talk to your agent — *"use zu to build a price-extraction agent, validate it,
run it, and show me what it does."* It calls `zu_scaffold` → `zu_validate` →
`zu_run` (which **streams every step back live**: train of thought, tool calls,
escalations) → `zu_traces` (read the full log of any run). See
[`examples/integrations/`](../examples/integrations/) for the exact configs.

## 9. Make it yours: custom plugins

Every built-in is a plugin behind a port, registered exactly the way yours would
be. The fastest path is the in-process decorator:

```python
import zu
from zu_core.registry import tool

@tool
class MyTool:
    name = "my_tool"
    tier = 1
    schema = {"name": "my_tool", "description": "...", "parameters": {"type": "object", "properties": {}}}
    prompt_fragment = "my_tool(): does the thing."
    async def __call__(self, ctx, **kwargs):
        return {"text": "..."}   # an observation

# then list it in your config's plugins.tools, or reference it by import path:
#   plugins: { tools: ["my_module:MyTool"] }
```

The other ports — `Detector`, `Validator`, `ModelProvider`, `SandboxBackend`,
`EventSink` — work the same way (`@detector`, `@validator`, …), or ship them as a
pip package that declares a `zu.*` entry point and Zu discovers it on install.

---

## Run it offline first

Every step above works with **no API key** using the deterministic `scripted`
provider — handy for tests and CI. Set the provider to:

```yaml
provider:
  name: scripted
  script: [{ text: '{"name": "Acme", "price": "$9"}', finish: stop }]
```

…and the loop will replay that answer with no network and no model. See
[`examples/killer_demo.py`](../examples/killer_demo.py) for the full
fetch → fail-on-JS → escalate → validate arc running entirely offline.
