<h1 align="center">🦓 Zu</h1>

<p align="center">
  <b>An opinionated, backend-agnostic runtime for agents that work in production.</b><br>
  Deterministic, auditable, and injection-resistant by construction — bring your own sandbox.
</p>

<p align="center">
  <a href="LICENSE"><img alt="License: Apache-2.0" src="https://img.shields.io/badge/license-Apache--2.0-blue.svg"></a>
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue.svg">
  <img alt="Tests passing" src="https://img.shields.io/badge/tests-passing-brightgreen.svg">
  <img alt="mypy clean" src="https://img.shields.io/badge/mypy-clean-brightgreen.svg">
</p>

<p align="center"><i>a pack of agents — escalation by image swap, not improvisation</i></p>

<!-- ▶ Hero demo: record `zu demo --offline --type escalation` (e.g. with vhs / asciinema),
     save it to docs/assets/zu-demo.gif, then uncomment the line below for an animated hero. -->
<!-- <p align="center"><img src="docs/assets/zu-demo.gif" alt="Zu: fetch → fail-on-JS → escalate → validated result" width="760"></p> -->

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

## 🚀 Quickstart

New here? One line gets you everything:

```bash
pip install 'zu-runtime[all]'     # web tools, both model SDKs, server, Docker, MCP
```

Or trim to what you need — a lean base, the heavy bits opt-in:

```bash
pip install zu-runtime                # base: import zu + the `zu` CLI + web tools
                                      #   (http_fetch/html_parse/render_dom) + provider
                                      #   adapters, detectors, validators, sqlite sink
pip install 'zu-runtime[anthropic]'   # + the Anthropic SDK to call a real model (also: [openai])
pip install 'zu-runtime[serve]'       # + the HTTP server (zu serve)
pip install 'zu-runtime[docker]'      # + the Docker sandbox (tier-2 browser containment)
pip install 'zu-runtime[mcp]'         # + the MCP server (zu mcp)
```

You almost never install the `zu-*` sub-packages individually — they're published standalone
so *plugin authors* can depend on just `zu-core`. As a user, install
`zu-runtime` (+ extras); `zu plugins` lists whatever you have.

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
server so Claude Code / Cursor / Codex can design, validate, run, inspect — and
**construct** — Zu agents for you in natural language, and stream the run back live.
One stdio server, every MCP client; register it once. See
[`examples/integrations/`](examples/integrations/).

**Build an agent cheaply.** A browser agent used to take many live, frontier-priced
iterations against a drifting site. Zu bounds that to **one** live capture: pathfind the
site once — `zu capture`, or drive `zu_explore` from your own harness so *your* discovery
becomes the agent's path — then iterate **offline at ~$0** against the recorded fixtures
(`zu run --offline`), harden the track against perturbations (`zu harden`), and gate it with
an executable anti-hardcode check (`zu construct`), all with no model and no network. The
flagship agent replays its track at **~$0.03/run** (vs ~$2.17 to pathfind). The full path is
the **[Building an agent guide](docs/agent-construction-sequence.md)**.

**→ The flagship example:**
[`examples/agents/vet-appointment/`](examples/agents/vet-appointment/) — search → fetch → a
persistent browser through a real booking widget → 3 grounded slots, with a recorded track
for deterministic replay.

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

## 🏗️ Architecture in one breath

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

Architecture, philosophy, and red-team deep-dives are published separately. In this repo:

- [`docs/agent-construction-sequence.md`](docs/agent-construction-sequence.md) — **the build-an-agent guide**: capture once → iterate offline at ~$0 → harden → ship
- [`examples/agents/vet-appointment/`](examples/agents/vet-appointment/) — the flagship example agent (start here)
- [`AGENTS.md`](AGENTS.md) — how to navigate and extend this repo (for AI agents and new humans)
- [`CLAUDE.md`](CLAUDE.md) — quick orientation for coding agents working in this repo
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — set up from a clone (`uv sync`), test, and submit changes

## License

[Apache-2.0](LICENSE). The open runtime is free and self-hostable forever; a
commercial control plane (hosted event store, audit & lineage UI, replay,
governance) lives in a separate repository and is the only thing outside this one.
