<h1 align="center">Zu</h1>

<p align="center">
  <b>An opinionated, backend-agnostic runtime for agents that work in production.</b><br>
  Deterministic, auditable, and injection-resistant by construction — bring your own sandbox.
</p>

---

> The name plays on *zoo* — a pack of different agents, each doing its own thing.

Building a production browser or web-research agent today means stitching search,
fetch, browsers, sandboxes, and models across several vendors — a fistful of API
keys just to reach "Hello World." None of those pieces is designed to work as a
system, so when something breaks you are debugging the seams between tools, not
your agent. And three failures recur: **reliability and cost**, **no auditability**,
and **prompt-injection exposure**.

Zu is the control logic the sandbox vendors tell you to build yourself. It sits
**above** the sandbox and treats it as an interchangeable backend, and it ships the
three disciplines no one else ships as a framework:

### The three pillars

- **Deterministic escalation.** A tier-blind harness whose capabilities are injected
  by container image. Escalation to a heavier tier (cheap HTTP → real browser) is
  owned by the orchestrator and triggered by deterministic detectors — never
  improvised by the model. One codebase; escalation by image swap.
- **Event-sourced provenance.** An append-only event log is the per-run system of
  record. Every run is lossless and replayable; OpenTelemetry and OpenLineage are
  derived, rebuildable views — not bolted-on, sampled debugging telemetry.
- **Capability-envelope security.** Capability acquisition is the orchestrator's job;
  the model may signal "I can't," never acquire. Injection-resistance is enforced by
  construction, not advised in a doc.

**Run it on any model.** The harness depends only on a `ModelProvider` port, so the
same build runs on Anthropic, OpenAI, OpenRouter, or a local model (Ollama / vLLM)
with a one-line config change.

## Status

The v1 core is complete and green: the typed contracts, the six ports, the
plugin registry, a scripted (fake) model provider, the event spine (SQLite sink
+ append-before-notify bus + projection), the deterministic interpreter loop
with tier-1 tools and budgets, the escalation ladder and the tier-2 browser,
validation against the event log (schema + grounding), the real `anthropic` and
`openai-compatible` model adapters, the **config system** (`zu run task.yaml`
wires a whole run from a file, the model a one-line swap), and the **killer
demo** — the full fetch → fail-on-JS → escalate → validated-result arc, runnable
with zero setup. What remains is breadth behind the existing ports, not new core.

## Quickstart

```bash
pip install zu-runtime            # the library + the `zu` CLI + every built-in plugin
pip install 'zu-runtime[all]'     # + HTTP server, Anthropic/OpenAI SDKs, Docker sandbox
zu plugins                        # list every discovered plugin across all six ports
zu demo                           # run the full arc instantly — no key, no network, no Docker
```

See the whole thing work the moment it's installed: `zu demo` fetches a JS page,
**fails on JavaScript, escalates to a browser**, and returns validated data —
offline and deterministic. Add `--provider anthropic --model claude-sonnet-4-6`
(with `ANTHROPIC_API_KEY` set, or `--api-key`) to watch a real model do it.

```
providers   anthropic, openai-compatible, scripted
tools       html_parse, http_fetch, render_dom
detectors   bot-wall, empty, error, js-shell
validators  grounding, schema
backends    local-docker
sinks       sqlite
```

Embed it in three lines — swap models by editing one config block:

```python
import zu
result = zu.run("task.yaml", config="zu.yaml")
print(result.status, result.value)
```

Or run it from the CLI, on a schedule, or as a service:

```bash
zu run task.yaml -c zu.yaml             # one-shot
zu run task.yaml -c zu.yaml --every 5m  # scheduled worker
zu serve -c zu.yaml                     # HTTP: POST /run   (needs the [serve] extra)
docker build -t zu . && docker run -p 8000:8000 -v "$PWD/zu.yaml:/app/zu.yaml" -e ANTHROPIC_API_KEY zu
```

**→ Full walkthrough: [`docs/QUICKSTART.md`](docs/QUICKSTART.md)** — install, define
a task + config, embed, serve, containerize, schedule, and write your own plugin.

Every built-in above is registered through the **same** plugin API you'd use for your
own — which is how we prove the plugin system is real, not a second-class add-on.

See the whole arc in one run — zero setup, no API key, no Docker (from a clone of
this repo):

```bash
python examples/killer_demo.py
```

It fetches a JS-heavy page, **fails on JavaScript, escalates to a browser**, and
returns **validated** structured data, then prints the queryable event log — all
three pillars in one run. Add `--provider anthropic --model claude-sonnet-4-6`
(with a key set) to watch a real model make the same escalation decision.

## The five-minute promise (real today)

A developer runs one command and watches an agent fetch a page, **fail on a
JavaScript site, escalate to a browser, and return validated structured data** —
with the event log queryable afterward. That single arc demonstrates all three
pillars in one run, and it ships as [`examples/killer_demo.py`](examples/killer_demo.py):
deterministic with the fake model and saved fixtures, or pointed at any real
model with one flag.

## Architecture in one breath

A tiny, stable **core** (`zu-core`: contracts, ports, registry, loop, bus) depends
only on the standard library and Pydantic — it physically cannot import a model SDK.
Everything that can vary is a **plugin behind a port**: models, tools, detectors,
validators, sandbox backends, and storage. The built-ins live in sibling packages
and register via entry points, exactly as your own pip package would.

```
zu/
  packages/
    zu-core/        # contracts, ports, registry, loop, bus   <- stable, tiny, SDK-free
    zu-providers/   # model adapters: scripted, anthropic, openai-compatible
    zu-tools/       # http_fetch, html_parse, render_dom
    zu-detectors/   # empty, error, js-shell, bot-wall
    zu-validators/  # schema, grounding
    zu-backends/    # local-docker sandbox + sqlite event sink
    zu-cli/         # the `zu` command + `zu serve` (HTTP)
    zu/             # the `import zu` embed facade (published as zu-runtime)
  examples/         # runnable demos (the killer demo lives here)
```

## Documentation

- [`docs/QUICKSTART.md`](docs/QUICKSTART.md) — install, define a task + config, embed, serve, containerize, schedule, and write your own plugin
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — set up from a clone (`uv sync`), test, and submit changes

## License

[Apache-2.0](LICENSE). The open runtime is free and self-hostable forever; a
commercial control plane (hosted event store, audit & lineage UI, replay,
governance) lives in a separate repository and is the only thing outside this one.
