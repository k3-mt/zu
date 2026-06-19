# Architecture

Zu is a small, stable **core** surrounded by six swappable **ports**. The core
owns only the loop, the registry, and the contracts. If a capability can vary,
it is a plugin behind a port — and the core depends only on the port's shape,
never on a concrete adapter.

## The stable core (`zu-core`)

Three things, and nothing else. It depends only on the standard library and
Pydantic, so it physically cannot import a model SDK.

- **Contracts** (`contracts.py`) — the typed boundaries everything speaks
  through: `TaskSpec` (typed input), `Result` (typed output), and the frozen
  `Event` envelope (the record). Event types are validated to the `harness.*` /
  `data.*` namespaces.
- **Registry** (`registry.py`) — the one registry the loop reads. Plugins enter
  it via entry points (installed packages), an in-process decorator, or — later —
  by reference in config.
- **The interpreter loop** (`loop.py`, build step 4) — ask the provider for an
  action, dispatch the tool by name, run detectors on the observation, repeat
  until the model finalises or the budget is spent; on finalise, run the
  validation ladder. The detector checkpoints are where escalation is decided.
- **The event bus** (`bus.py`, build step 3) — append-before-notify: every event
  is persisted before any subscriber is notified, so the log is the source of
  truth and a crashing subscriber can't lose a record.

## The six ports

Each is a narrow, runtime-checkable `Protocol` in `zu_core.ports`, with built-in
adapters in a sibling package and an entry-point group through which users add
their own.

| Port | Responsibility | Entry-point group · built-ins |
|------|----------------|-------------------------------|
| `ModelProvider` | Turn a normalized request into a response (text + tool calls) | `zu.providers` · scripted, anthropic, openai-compatible |
| `Tool` | An action the model may take | `zu.tools` · http_fetch, html_parse, render_dom |
| `Detector` | A judgment about an observation; returns a `Verdict` | `zu.detectors` · empty, error, js-shell, bot-wall |
| `Validator` | An on-final check of the result | `zu.validators` · schema, grounding |
| `SandboxBackend` | Provision and run a tier's environment | `zu.backends` · local-docker |
| `EventSink` | Persist and query the event log | `zu.sinks` · sqlite, jsonl |

## The capability envelope

A `Tool` declares more than its schema and tier: it declares the **capabilities**
it needs (`capabilities`, least-privilege tokens like `CAP_NET`/`CAP_SANDBOX`) and
its **egress** allowlist (`egress`, specific hosts; `{EGRESS_OPEN}` only for the
reviewed open-internet case, empty for none). The loop records every active tool's
declared envelope to the log at run start (`harness.envelope.declared`). This is
the machine-readable contract the secure-by-default thesis rests on: the
declaration is the *what*, a `SandboxBackend` is the *enforcement*, and the gate's
out-of-band observers judge observed behaviour against the declaration. See
[`PHILOSOPHY.md`](PHILOSOPHY.md) §5–6.

## The plugin-test gate

A seventh package, `zu-redteam`, is the gate that proves a plugin cooperates and
withstands attack before it ships — itself a Zu agent fleet (Zu on both sides).
It is reached with `zu test-plugin <pkg>` and runs the graded gates (unit ·
contract · interop · adversarial) with deterministic, out-of-band verdict
observers. It is test/CI infrastructure, not part of the runtime a deployed agent
loads. See [`RED_TEAM.md`](RED_TEAM.md).

## The any-model seam

`ModelProvider` is the port that makes "run on any model" true. The harness
speaks one normalized `ModelRequest`/`ModelResponse`; an adapter translates to
each provider's wire format and declares its `Capabilities`. Because OpenRouter,
OpenAI, and local servers (Ollama, vLLM) all expose an OpenAI-compatible
endpoint, a single `openai-compatible` adapter — pointed at a different base URL —
covers a vast range of models. Anthropic gets its own adapter. If a model lacks
native tool-calling, the adapter falls back to a prompt-based tool protocol, so
the same harness still works. The core never special-cases a provider; it reads
capabilities and proceeds. **Credentials are resolved from the environment inside
the adapter — never placed in the model's context or in config files.**

## Plugin discovery — three ways, one registry

1. **Installed packages, via entry points.** A package declares its plugins in
   `pyproject.toml`; Zu discovers them on startup with no code change. This is how
   the built-ins register and how a user ships their own.
2. **In-process, via a decorator.** `@zu.tool` / `@zu.detector` / … registers a
   plugin in your own script — no packaging required.
3. **By reference in config** (build step 8). The config file names a plugin by
   import path, to activate or order plugins per tier without touching code.

All three resolve into the same registry the loop reads.

## Why this shape

The point of the port discipline is that the v1 is small and shippable, and the
full production system is reachable by **adding adapters and subscribers — never
by reopening the core**. See [`BUILD.md`](BUILD.md) for what each seam defers.
