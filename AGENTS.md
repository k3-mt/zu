# AGENTS.md — how to work in this repository

This file is the entry point for an AI agent (or a new human) working in Zu. It
is the *how*; the *why* (philosophy) and the *shape* (architecture) are in the
documentation, published separately. Read this first.

Zu is a runtime for agents, and its own repository is designed to be navigated
by one. The promise is **one predictable shape, so "where does X live?" has
exactly one answer.** Keep it that way.

## The 30-second model

A tiny, stable **core** (`zu-core`) owns the contracts, the registry, the
interpreter loop, and the event bus — and depends only on the standard library
and Pydantic, so it physically cannot import a model SDK. Everything that can
vary is a **plugin behind a typed port**: models, tools, detectors, validators,
sandbox backends, and event sinks. Built-ins live in sibling packages and
register through the *same* entry points your own package would use.

## Repository layout

```
zu/
  AGENTS.md                 # you are here
  CONTRIBUTING.md           # human-facing setup + submission flow (same rules)
  packages/
    zu-core/                # contracts (+ multimodal Content/Observation/Action), ports, registry, loop, bus <- stable, SDK-free
    zu-providers/           # model adapters: scripted, anthropic, openai-compatible; LlmPolicy (the Policy bridge)
    zu-tools/               # http_fetch, html_parse, render_dom, action_surface, pointer, simulate
    zu-checks/              # detectors (empty, error, js-shell, bot-wall, action-surface-blind) + validators (schema, grounding)
    zu-backends/            # local-docker sandbox + sqlite/jsonl event sinks + triggers (webhook/queue/schedule/object-store)
    zu-huggingface/         # HuggingFace task models as typed tools/detectors/validators + supply-chain guards
    zu-redteam/             # the plugin-test gate + adversarial red-team agent
    zu-cli/                 # the `zu` command, HTTP server, MCP server
    zu/                     # the `import zu` embed facade (published as zu-runtime)
    zu-testing/             # shared test kit: fakes, fixtures, the pytest plugin
  examples/                 # runnable example agents + integration configs
  validation/               # end-to-end proof suites (containment, red-team)
  automation/               # zu maintaining zu — the gap-triage agent run from CI
  community/                # community infra (the Discord bot) — outside the package workspace
```

Every package has the **same internal shape** — rely on this:

```
packages/zu-<name>/
  pyproject.toml            # declares entry points — how Zu discovers the plugin
  README.md                 # what it is + the port it implements + plugin names
  src/zu_<name>/            # the source (one module per plugin)
  tests/                    # test_<module>.py, deterministic + offline
```

## The dev loop (run these)

```bash
uv sync                     # create the env, install every workspace package editable
uv run pytest               # the whole suite — no API keys, no network
uv run mypy packages        # type-check the ports and contracts
uv run ruff check packages  # lint
uv run zu plugins           # sanity-check plugin discovery
```

The bar for any change is simple and non-negotiable: **the offline suite stays
green, mypy stays clean, ruff stays clean.** Every change ships with a test that
needs no live model and no live network — use the `ScriptedProvider` (fake
model) and saved web fixtures.

## The ports → where a plugin goes

Each port is a runtime-checkable `Protocol` in `zu_core.ports`. You implement a
*shape*, not a base class.

| You want to add…            | Put it in…       | Entry-point group | Port            |
|-----------------------------|------------------|-------------------|-----------------|
| a model adapter             | `zu-providers`   | `zu.providers`    | `ModelProvider` |
| a tool the model can call   | `zu-tools`       | `zu.tools`        | `Tool`          |
| a detector (escalation)     | `zu-checks`      | `zu.detectors`    | `Detector`      |
| an on-final result check    | `zu-checks`      | `zu.validators`   | `Validator`     |
| a sandbox backend           | `zu-backends`    | `zu.backends`     | `SandboxBackend`|
| an event sink (storage)     | `zu-backends`    | `zu.sinks`        | `EventSink`     |
| a policy (the decision-maker) | `zu-providers` | `zu.policies`     | `Policy`        |
| typed vendor/product discovery | `zu-providers` | `zu.retrieval_providers` | `RetrievalProvider` |
| a computed merchant-trust score | `zu-providers` | `zu.reputation_providers` | `ReputationProvider` |
| a trigger (inbound event)   | `zu-backends`    | `zu.triggers`     | `Trigger`       |
| a pre-execution gate        | (your pkg)       | `zu.gates`        | `InvocationGate`|
| a harness-owned channel     | `zu-backends`    | `zu.channels`     | `Channel`       |
| a workload identity         | `zu-backends`    | `zu.workload_identity` | `WorkloadIdentity`|
| egress enforcement          | `zu-backends`    | `zu.egress_enforcement`| `EgressEnforcement`|

The last four are the **upstream-conformance** seams (see
[`zu-upstream-conformance.md`](zu-upstream-conformance.md) and the trusted-base
enumeration in [`docs/TCB.md`](docs/TCB.md)): a deterministic pre-execution gate
(`InvocationGate`, ZU-CORE-2) that returns allow/deny/escalate before a tool
runs; a generalised harness-owned `Channel` (ZU-NET-2) — the credential broker is
the proof case, run out-of-process via `zu_backends.oop_launcher` for a real
memory boundary (ZU-NET-3); attestable `WorkloadIdentity` (ZU-NET-4/5); and
pluggable `EgressEnforcement` (ZU-NET-1). New port *kinds* register at runtime via
`Registry.register_kind` (or the `zu.kinds` entry-point group) **without editing
the core** (ZU-EXT-1).

The last two are the policy-agnostic seams (Engineering Design §9.2, §4.4):
`Policy` (`act(observation, tools) -> Action`) is the generalisation of the
decision-maker — an LLM is bridged onto it by `LlmPolicy`, a world model or
embodied controller implements it directly; `Trigger` (`listen()`) is the
inbound mirror of `EventSink`, carrying **untrusted** payloads. Both the
observation and the action are typed multimodal `Content` (`Text`/`Image`/
`Audio`) in `zu_core.content`.

The **open-web research** seams (the vendor-research capability set, #81–84) are the
discovery siblings of `ModelProvider`: `RetrievalProvider` (`search(query) ->
list[Candidate]`) FINDS the site and returns typed candidate facts, never prose;
`ReputationProvider` (`assess(domain) -> ReputationVerdict`) is a computed,
auditable merchant-trust decision over hard-to-forge domain signals (a deterministic
reference scorer ships in `zu-providers`). Two more pieces compose with them:
`zu_tools.extract` (#82) turns a fenced `ContentView` into typed facts against a
schema (the safe read→decide bridge — only typed values flow downstream, never
injected instructions), and the **quarantined run-mode** (#83, `TaskSpec(...,
quarantined=True)`) is a provable tool-less/egress-free reader — the loop offers an
empty tool set and refuses any tool call as a high-signal escape attempt, so
injection in untrusted content is contained to a data-integrity problem, never an
action. Quarantine is structural, not by convention: the effective tool set is
empty, so the run declares zero egress and can never breach a `containment="required"`
posture, and it must not share a grant store / execution ledger (sharing fails loud).
The named backstop is `packages/zu-core/tests/test_quarantine.py` (ZU-CORE-6).

Plugins are discovered three ways, all resolving into one registry: installed
packages via entry points (`pyproject.toml`), the in-process decorators
(`@zu.tool` / `@zu.detector` / …), and by import-path reference in config.

## Recipe: add a tool

1. Create `packages/zu-tools/src/zu_tools/<name>.py` with a class that implements
   the `Tool` shape:

   ```python
   from zu_core.ports import CAP_NET, EGRESS_OPEN  # only what you actually need

   class MyTool:
       name = "my_tool"
       tier = 1                       # cheapest tier; climbs only via a detector ESCALATE
       schema = {"name": "my_tool", "description": "...", "parameters": {...}}
       prompt_fragment = "my_tool(arg): does the thing."
       # The capability envelope — least privilege, declared (see below).
       capabilities = frozenset()     # e.g. {CAP_NET} if you open the network
       egress = frozenset()           # e.g. {EGRESS_OPEN} for a general web fetcher
       async def __call__(self, ctx, **kwargs) -> dict:
           return {"text": "..."}     # an observation
   ```

2. Register it under `zu.tools` in `packages/zu-tools/pyproject.toml`.
3. Add `tests/test_<name>.py` — deterministic, offline.
4. `uv run pytest && uv run mypy packages && uv run ruff check packages`.

The other ports follow the same pattern; see each package's README for the
shape it expects.

## The design invariants (do not violate)

- **The core stays small and SDK-free.** `zu-core` imports only stdlib +
  Pydantic. Never add a model SDK, a browser, or a concrete adapter to it.
- **Capability lives in plugins, never the core.** If you are adding a domain
  branch to the core, it belongs in a plugin behind a port.
- **Declare your capability envelope.** A `Tool` declares `capabilities`
  (least-privilege tokens like `CAP_NET`) and `egress` (its host allowlist;
  `{EGRESS_OPEN}` only for the reviewed open-internet case, empty for none).
  The loop records this on the event log at run start
  (`harness.envelope.declared`) so the gate's out-of-band verdict observers can
  judge behaviour against the declaration. Declaring more than you need is the
  smell the gate is built to catch.
- **The event log is the source of truth.** Everything that matters emits to it
  (`harness.*` / `data.*` types in `zu_core.events`); it is the audit trail and
  the test oracle at once. Don't mutate a published event's payload.
- **Explicit over implicit.** No hidden magic, no action-at-a-distance. What you
  see is what runs.
- **Fail loudly, not silently.** Validate inputs and outputs; surface errors
  (log or an error observation), never swallow them into a misleading success.

## The plugin test gate

A plugin is not "done" when its unit tests pass — it is done when it has been
proven to cooperate with other plugins and to withstand an adversarial red team,
inside a real Zu runtime. That gate lives in `packages/zu-redteam` and is run
with `zu test-plugin <pkg>` (the red-team gate design + status is in the
published docs).

## Pointers

- The flagship example agent → [`examples/agents/vet-appointment/`](examples/agents/vet-appointment/) (machinery fixture agents live in `packages/zu-cli/tests/agents/`)
- Building an agent (capture → offline at ~$0 → harden → ship) → [`docs/agent-construction-sequence.md`](docs/agent-construction-sequence.md)
- Quick orientation for coding agents → [`CLAUDE.md`](CLAUDE.md)
- End-to-end proof suites (containment, red-team) → [`validation/`](validation/)
- The shared test kit for your own plugins → [`packages/zu-testing/`](packages/zu-testing/)
- Human setup + PR flow → [`CONTRIBUTING.md`](CONTRIBUTING.md)
- Architecture, philosophy, red-team → the published documentation
