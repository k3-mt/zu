# Build an agent, end to end

The guided path from nothing to a deployed, tested, red-teamed agent. Follow it
top to bottom once; afterwards [`QUICKSTART.md`](QUICKSTART.md) is the reference
for each surface in depth.

> **The mental model.** You write two files. A **task** (`task.yaml`) is *what you
> want* — a query, a target, and the JSON shape of the answer. A **config**
> (`zu.yaml`) is *how to run it* — which model, which plugins, the escalation
> ceiling, where the event log goes. The runtime drives a tool-using loop to a
> validated `Result` and records every step as an event. You never write loop code.

```
 zu init ─▶ edit task.yaml + zu.yaml ─▶ zu run ─▶ test ─▶ red-team ─▶ zu deploy
 (scaffold)   (what + how)            (watch it)  (offline) (contained)  (ship)
```

---

## 0. Install

```bash
pip install 'zu-runtime[all]'     # everything; or pick extras — see QUICKSTART §1
zu plugins                        # list what's installed and discoverable
```

Python 3.11+ is the only hard requirement. You need an API key only to hit a real
model, and Docker only when a run actually escalates to the tier-2 browser.

## 1. Scaffold the agent

```bash
mkdir my-agent && cd my-agent
zu init --template web            # web | minimal | research → writes zu.yaml + task.yaml
```

You now have a runnable pair. `web` is the full shape (fetch → fall back to a
browser on JS → validate); `minimal` is a model answering JSON with no tools.
There's a worked copy at [`examples/agents/price-extractor`](../examples/agents/price-extractor).

## 2. Say what you want — `task.yaml`

```yaml
query: "Extract the product name and price from the page."
target: "https://shop.example/products/aeropress-go"
max_tier: 2                       # how high the escalation ladder may climb (see §4)
output_schema:                    # the answer MUST fit this shape…
  type: object
  properties:
    name:  { type: string }
    price: { type: string }
  required: [name, price]
budget: { max_steps: 12, max_tokens: 100000, wall_time_s: 60 }
```

The `output_schema` is enforced by the `schema` validator, and every value in it
is checked against the fetched page by `grounding` — so a fabricated price is
refused, not returned as success.

## 3. Choose the model — and supply the key

The `provider` block in `zu.yaml` is the **one-line model swap**; the loop only
ever speaks to a provider port, so nothing else changes.

```yaml
provider:
  name: anthropic                 # scripted | anthropic | openai-compatible | <module:Class>
  model: claude-sonnet-4-6
  api_key_env: ANTHROPIC_API_KEY  # the env var NAME — never the key itself
```

Swap to OpenRouter, or a local model, by editing only this block:

```yaml
provider:
  name: openai-compatible
  model: "anthropic/claude-3.5-haiku"     # any model the endpoint serves
  base_url_env: OPENROUTER_BASE_URL       # or OPENAI_BASE_URL=http://localhost:11434/v1 (Ollama)
  api_key_env: OPENROUTER_API_KEY
```

**Credentials never live in a file.** `api_key_env` names the environment variable
that holds the key; the adapter reads it at call time. Set it in your shell (or a
secret manager / platform secret in production):

```bash
export ANTHROPIC_API_KEY=sk-...
```

Embedding in your own code and already holding a key in memory? Pass `api_key`
directly in a **config dict** (never a committed YAML) — see QUICKSTART §4.

## 4. Design the escalation — tiers, detectors, models

This is the part that makes Zu different: **escalation is deterministic and owned
by the orchestrator, never improvised by the model.** Three pieces:

**Tiers (capability by image).** Every tool carries a `tier`. The loop only offers
the model the tools at or below the current tier — it starts at tier 1
(`http_fetch`, `html_parse`) and withholds the heavier tier-2 `render_dom` (a real
browser) until the ladder climbs. The model can *signal* "I'm stuck," never reach
for a capability it wasn't given.

**Detectors decide the climb.** After each observation the loop runs the
detectors. A verdict's severity drives control flow:

| Verdict | What the loop does |
|---|---|
| `ESCALATE` | climb one tier, unlock its tools, let the model retry |
| `TERMINAL` | end the run (a dead page — don't waste a tier) |
| `RETRY` / `WARN` | continue; the model sees the observation and decides |

So a JS-only shell page fires `js-shell → ESCALATE`, the loop climbs 1→2,
`render_dom` becomes available, and the same job retries with a real browser.
Pick the detectors that match your failure modes:

```yaml
plugins:
  tools:      [http_fetch, html_parse, render_dom]
  detectors:  [empty, error, js-shell, bot-wall]   # what makes tier 1 give up
  validators: [schema, grounding]
```

**The ceiling.** `task.max_tier` caps how high it climbs; the effective ceiling is
the lower of `max_tier` and the highest tier any registered tool actually
occupies (so it never climbs to an empty tier). Set `max_tier: 1` to forbid the
browser entirely (no Docker ever).

**A different model per tier (optional).** Run a cheap/fast model at tier 1 and a
frontier or vision model only once you've escalated — the neutral message format
lets a different adapter pick up the same conversation:

```yaml
provider:                          # the global default (every tier unless overridden)
  name: openai-compatible
  model: "anthropic/claude-3.5-haiku"
  base_url_env: OPENROUTER_BASE_URL
  api_key_env: OPENROUTER_API_KEY
providers:                         # per-tier overrides, keyed by tier number
  2:                               # on escalation to the browser tier, switch up
    name: anthropic
    model: claude-sonnet-4-6
    api_key_env: ANTHROPIC_API_KEY
```

## 5. Run it — and watch it think

```bash
zu run task.yaml -c zu.yaml
```

A live trace streams as the loop runs — the model's reasoning, each tool call and
result, detector verdicts, and escalations:

```
  ▶ task: Extract the product name and price. → https://…
  🔧 http_fetch({'url': 'https://…'})   📄 fetched 1024 chars (status 200)
  🔎 detector js-shell [escalate] — page appears to be a JS shell
  ⬆️  ESCALATE 1→2: js-shell — climbing a tier
  📦 extracted: {'name': 'Acme Widget', 'price': '$9.00'}   ✅ completed
```

`--no-stream` for CI. A non-success run exits non-zero, so it composes in a shell.
Every step is on the event log (`zu.db`) — queryable, replayable provenance.

## 6. Test it — offline first, then for real

**Wire-check with no key, no network.** Copy `zu.yaml` to `zu.offline.yaml` and
point its provider at the deterministic `scripted` model — the loop replays a
canned answer with no model and no network:

```yaml
# zu.offline.yaml
provider:
  name: scripted
  script: [{ text: '{"name": "Acme", "price": "$9"}', finish: stop }]
```

```bash
zu run task.yaml -c zu.offline.yaml      # proves the wiring with no spend
```

**Write a real test for your agent.** The `zu-testing` kit runs your tools +
validators through the *real* loop against a fixture page — no model, no network:

```python
# pip install zu-testing
async def test_my_agent(agent_runner, make_fetch_tool):
    result, events = await agent_runner(
        [{"tool": "http_fetch", "args": {"url": "https://shop.example/x"}},
         {"text": '{"name": "AeroPress Go", "price": "$39.95"}', "finish": "stop"}],
        tools={"http_fetch": make_fetch_tool(text=open("fixtures/product.html").read())},
    )
    assert result.status.value == "success"
```

See [`examples/agents/price-extractor`](../examples/agents/price-extractor) and
the worked test at `packages/zu-cli/tests/test_example_agents.py` for the full
pattern (success, fabrication refused, config validity). The test lanes:

```bash
make test          # fast & hermetic — no network, no Docker (the default gate)
make test-docker   # + the contained, in-container proofs
make test-live     # + real models (needs keys)
```

## 7. Red-team it — end to end

Two layers, both shipped.

**Certify a custom plugin.** If you wrote your own tool/detector/validator, run it
through the adversarial gate — it stands your plugin up in a real runtime with
neighbours, attacks it with a frozen corpus, and judges the result on out-of-band
evidence, passing only if the declared capability envelope held:

```bash
pip install 'zu-runtime[test]'
zu test-plugin my-plugin-package          # unit · contract · interop · adversarial
zu test-plugin my-plugin-package --watch  # stream each attack live
```

**Contain the whole agent.** For a run with untrusted tools, set the containment
posture and run the entire agent inside a hardened container whose only route
off-box is an egress proxy on an internal, default-DROP network:

```yaml
# zu.yaml
containment: required        # refuse to run a capability tool UNLESS contained
```

```bash
zu run task.yaml -c zu.yaml --sandboxed   # whole agent in a box, behind the proxy
```

`required` fails closed on a bare host (it won't silently run a network/tool
unguarded); the launcher establishes the boundary and the full in-container event
log still surfaces back to you. Prove the boundary holds end to end:

```bash
cd validation/containment && ./run_all.sh   # build · floor · agent-in-box · egress · proxy
```

Background: [`RED_TEAM.md`](RED_TEAM.md) · [`RED_TEAM_CONTAINER.md`](RED_TEAM_CONTAINER.md)
· [`PHILOSOPHY.md`](PHILOSOPHY.md) (the capability-envelope model).

## 8. Build & deploy

`zu deploy` turns the config into a running HTTP service. Secrets are never baked
in — the provider's key env is passed at run time (local) or referenced as a
platform secret (cloud).

```bash
zu deploy local                 # generate a Dockerfile, build, run → http://localhost:8000
zu deploy local --dry-run       # just print the docker commands

zu deploy compose               # docker-compose.yml
zu deploy fly                   # fly.toml      → fly secrets set …, fly deploy
zu deploy render                # render.yaml   → create a Blueprint
zu deploy dockerfile            # just the Dockerfile
```

The service exposes `POST /run`, `POST /run/stream` (live SSE), a dashboard at
`/`, and `GET /healthz`. To run it yourself instead of `zu deploy`:

```bash
zu serve -c zu.yaml --host 0.0.0.0 --port 8000     # needs ZU_SERVE_TOKEN to bind non-localhost
```

## 9. Operate it

```yaml
# zu.yaml — ship a copy of every event somewhere you can watch it in production
event_sink:  { driver: sqlite, path: ./zu.db }          # canonical store
trace_sinks: [{ driver: jsonl, path: /var/log/zu/trace.jsonl }]  # for log shippers
observability: { scope: render }                         # redact content on networked feeds
```

```bash
zu run task.yaml -c zu.yaml --every 5m       # built-in interval worker
```

Watch live at `http://localhost:8000/` (the dashboard) — train of thought, tools,
escalations, and a **Defenses** panel of any contained attack.

## Multi-phase agents — chaining runs robustly

One run produces one *validated, replayable* Result. A multi-phase agent — extract,
then summarize, then decide — is a **sequence of runs**, and the robust way to
chain them is `zu.Pipeline`, which lifts a single run's guarantees to the whole
sequence: every phase shares one `trace_id` and one event log, a phase advances
only on the previous one's validated success, and a re-run **resumes from the log**
instead of repeating finished work.

```python
import zu

pipe = zu.Pipeline(config="zu.yaml")                 # one trace, one shared log
pipe.phase("extract",   {"query": "Extract the topic and one key point.",
                         "output_schema": {...}})
pipe.phase("summarize", lambda prev: {               # consumes phase 1's value
    "query": f"Summarise: {prev.value}", "output_schema": {...}})

result = pipe.run()           # PipelineResult: status, value, phases, events, id
```

- **Gate** — `summarize` runs only if `extract` finished `SUCCESS` (the validators
  decide "satisfied"); on failure the pipeline stops with the log intact.
- **One lineage** — `result.events` is the whole pipeline under `result.id`, so it
  replays as a unit; each phase is still queryable on its own `task_id`.
- **Resume** — give the config a durable `event_sink` and a stable `pipeline_id`;
  re-running skips phases already on the log and reuses their values.

Pipelines are *code* (not a `task.yaml`) on purpose — each phase stays
independently validated, budgeted, and auditable, so you get staging without
giving up provenance. Runnable example:
[`examples/agents/research-pipeline`](../examples/agents/research-pipeline)
(`python pipeline.py` — offline, no key). For *model-driven* branching (the model
chooses the next phase), make each phase a **tool** it calls inside one run.

---

## Where to go next

- [`QUICKSTART.md`](QUICKSTART.md) — reference for every surface (embed, serve, MCP, schedule, custom plugins)
- [`ARCHITECTURE.md`](ARCHITECTURE.md) — the small core and the six ports
- [`../packages/zu-testing/README.md`](../packages/zu-testing/README.md) — the test kit for your own plugins
- Drive it from Claude Code / Cursor / Codex over MCP — QUICKSTART §9
