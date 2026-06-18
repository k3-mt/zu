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

Early. The core is being built in the open in nine steps (see
[`docs/BUILD.md`](docs/BUILD.md)). **Steps 1–2 are done and green:** the typed
contracts, the six ports, the plugin registry, and a scripted (fake) model provider
that makes the whole runtime testable offline. The interpreter loop, escalation,
tier-2 browser, validation, and real model adapters are next.

## Quickstart (for contributors today)

```bash
git clone https://github.com/<you>/zu && cd zu
uv sync                 # create the env, install every workspace package editable
uv run pytest           # the offline suite — no API keys, no network
uv run zu plugins       # list every discovered plugin across all six ports
```

```
providers   anthropic, openai-compatible, scripted
tools       html_parse, http_fetch, render_dom
detectors   bot-wall, empty, error, js-shell
validators  grounding, schema
backends    local-docker
sinks       sqlite
```

Every built-in above is registered through the **same** plugin API you'd use for your
own — which is how we prove the plugin system is real, not a second-class add-on.

## The five-minute promise (the target for v1)

A developer runs `pip install`, writes a few lines, and watches an agent fetch a
page, **fail on a JavaScript site, escalate to a browser, and return validated
structured data** — with the event log queryable afterward. That single arc
demonstrates all three pillars in one run.

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
    zu-cli/         # the `zu` command
  examples/         # runnable demos (the killer demo lives here)
  docs/             # design, build sequence, architecture
```

## Documentation

- [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) — the ports-and-adapters design
- [`docs/BUILD.md`](docs/BUILD.md) — the nine-step build sequence, each step testable offline
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — set up, test, and submit changes

## License

[Apache-2.0](LICENSE). The open runtime is free and self-hostable forever; a
commercial control plane (hosted event store, audit & lineage UI, replay,
governance) lives in a separate repository and is the only thing outside this one.
