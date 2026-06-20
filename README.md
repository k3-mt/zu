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
  the model may signal "I can't," never acquire — and the orchestrator, not the model,
  owns tool dispatch and tier gating. In the base runtime each tool *declares* its
  capability envelope (egress, capabilities) and every declaration and every contained
  block is recorded on the event log — **audited by construction**. Turning that
  declaration into a hard *boundary* a hostile tool cannot cross (default-deny egress,
  filesystem/syscall limits) is the job of a sandbox backend: real, and enforced in the
  Docker/container path (`zu-backends`, the red-team container form), but **opt-in and
  off by default**. Run untrusted tools behind that backend — do not assume the base
  runtime contains a tool just because it declares a narrow envelope.

### Containing a run

Containment is two problems, and they have different answers:

- **A rogue/injected model** is contained *in-process, by construction*: the model
  only ever signals an action — the orchestrator owns dispatch and tier escalation,
  and only offers tools unlocked at the current tier. A prompt-injected model cannot
  acquire a capability it wasn't given. This holds in the base runtime today.
- **A hostile *tool* (supply chain)** cannot be contained in-process — a tool is code
  running in your interpreter. Real tool containment is an OS boundary, so it's a
  deployment posture:

  ```yaml
  # zu.yaml
  containment: required   # fail closed — see below
  ```

  With `containment: required`, the runtime **refuses to run** any tool with off-box
  reach (declared egress/capabilities, or tier ≥ 2) unless the run is executing
  inside the Zu sandbox — it will not silently run a capability-bearing tool
  unguarded. To actually run such a tool, launch the **whole agent inside a hardened
  container** (default-DROP network, egress only via a logging allowlist proxy, all
  caps dropped, blocking seccomp):

  ```bash
  zu run task.yaml -c zu.yaml --sandboxed   # needs Docker + the zu image
  ```

  Inside that box the container — not the loop — is the boundary, so the tool runs
  contained. `containment: audit` (the default) runs tools in-process and logs every
  declaration; right for trusted tools, where tier-1's own SSRF/DNS-pin guards apply.

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

A lean base, plugins opt-in (dbt-style):

```bash
pip install zu-runtime            # base: core + CLI + import zu + provider adapters,
                                  #       detectors, validators, sqlite event sink
pip install 'zu-runtime[web]'     # + web tools (http_fetch, html_parse, render_dom)
pip install 'zu-runtime[anthropic]'   # + Anthropic SDK     (also: [openai])
pip install 'zu-runtime[serve]'       # + HTTP server (zu serve)
pip install 'zu-runtime[all]'         # everything (web + both SDKs + server + Docker)
```

Each plugin is also a standalone package (`pip install zu-tools`, `zu-providers`, …),
the way dbt ships adapters. `zu plugins` lists whatever you've installed.

Prove it runs — `zu demo` runs against a **real model** (that's the point: prove
runnability, not just wired logic):

```bash
export ANTHROPIC_API_KEY=sk-...
pip install 'zu-runtime[demo,anthropic]'
zu demo --model claude-sonnet-4-6     # real http_fetch + extract + validate (tier 1)
zu demo --type minimal --model claude-sonnet-4-6   # no tools — needs only a key
zu demo --offline                     # scripted self-test (no key) — proves wiring, not a real run
```

**Prerequisites — the requirement ladder:** Python 3.11+ (always) · an **API key**
for a real model · **+ network** for tier-1 web tools (`http_fetch`/`html_parse`) ·
**+ Docker** only for the tier-2 browser (`render_dom`). So tier 1 needs *network,
not Docker*; only the real browser tier needs Docker.

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
zu run task.yaml -c zu.yaml             # one-shot — streams a LIVE trace as it runs
zu run task.yaml -c zu.yaml --every 5m  # scheduled worker
zu serve -c zu.yaml                     # HTTP: POST /run  ·  POST /run/stream (live SSE)
docker build -t zu . && docker run -p 8000:8000 -v "$PWD/zu.yaml:/app/zu.yaml" -e ANTHROPIC_API_KEY zu
```

**Watch it think, live.** Every run streams its train of thought — the model's
reasoning, each tool call and result, detector verdicts, and escalations — to the
console as it happens (`zu run`), or over Server-Sent Events (`POST /run/stream`)
so you can watch a local or containerized run in real time, no refresh.

**Live in your coding agent.** `pip install 'zu-runtime[mcp]'` adds a `zu mcp`
server so Claude Code / Cursor / Codex can design, validate, run, and inspect Zu
agents for you in natural language — and stream the run back live. One stdio
server, every MCP client; register it once. See
[`examples/integrations/`](examples/integrations/).

**→ Runnable examples:** [`examples/agents/`](examples/agents/) — from one-shot
extraction to a multi-phase pipeline, each tested offline.

Every built-in above is registered through the **same** plugin API you'd use for your
own — which is how we prove the plugin system is real, not a second-class add-on.

## The five-minute promise (real today)

See the whole arc in one command — zero setup, no API key, no Docker:

```bash
zu demo --offline --type escalation
```

It fetches a page, **fails on JavaScript, escalates to a browser**, and returns
**validated** structured data, then prints the queryable event log — all three
pillars in one run, deterministic with the fake model and saved fixtures. Drop
`--offline` and add `--model claude-sonnet-4-6` (with a key) to watch a real model
make the same escalation decision.

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
    zu-checks/      # detectors (empty, error, js-shell, bot-wall) + validators (schema, grounding)
    zu-backends/    # local-docker sandbox + sqlite/jsonl event sinks
    zu-redteam/     # the plugin-test gate + adversarial red team (zu test-plugin)
    zu-cli/         # the `zu` command + `zu serve` (HTTP)
    zu/             # the `import zu` embed facade (published as zu-runtime)
    zu-testing/     # shared test kit (fakes, fixtures, pytest plugin)
  examples/         # runnable example agents + integration configs
  validation/       # end-to-end proof suites (containment, red-team)
```

## Documentation

Full documentation (quickstart, the build-an-agent guide, architecture,
philosophy, red-team) is published separately. In this repo:

- [`examples/agents/`](examples/agents/) — runnable example agents (start here): one-shot extraction and a multi-phase pipeline
- [`AGENTS.md`](AGENTS.md) — how to navigate and extend this repo (for AI agents and new humans)
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — set up from a clone (`uv sync`), test, and submit changes

## License

[Apache-2.0](LICENSE). The open runtime is free and self-hostable forever; a
commercial control plane (hosted event store, audit & lineage UI, replay,
governance) lives in a separate repository and is the only thing outside this one.
