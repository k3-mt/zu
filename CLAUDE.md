# CLAUDE.md — orientation for Claude Code (and any coding agent)

Zu is a backend-agnostic runtime for production agents. This file is the quick orientation;
the canonical, fuller guides are:

- **[AGENTS.md](AGENTS.md)** — how to work in this repo (layout, the six ports, recipes, the
  design invariants). **Read it before changing code.**
- **[README.md](README.md)** — what Zu is and why (the three pillars, quickstart).
- **[docs/agent-construction-sequence.md](docs/agent-construction-sequence.md)** — the guide to
  building a full agent cheaply (capture once → iterate offline at ~$0 → harden → ship).

## 30-second model

A tiny, SDK-free **core** (`zu-core`: contracts, ports, registry, loop, bus) owns the
interpreter loop and the event log. Everything that varies is a **plugin behind a typed port**
(models/**policies**, tools, detectors, validators, sandbox backends, event sinks, **triggers**),
discovered via entry points. Built-ins live in sibling `packages/zu-*`. The core also carries the
typed multimodal currency (`Content`/`Observation`/`Action` in `zu_core.content`) the
policy-agnostic seams speak. One predictable shape — see AGENTS.md.

## The dev loop — the non-negotiable bar

```bash
uv sync                         # editable install of every workspace package
uv run pytest                   # the whole suite — NO api keys, NO network, NO Docker
uv run mypy packages            # types
uv run ruff check packages      # lint
```

**Every change keeps the offline suite green, mypy clean, ruff clean**, and ships with a test
that needs no live model and no network — use the `ScriptedProvider` (fake model) + saved
fixtures. The Docker-gated tests run only with `--run-docker` (e.g. `ZU_SANDBOX_IMAGE=zu:test
uv run pytest --run-docker`).

## Two disciplines that are load-bearing here

- **Generic capabilities, never site-specific hardcoding.** When the model hits a wall, the
  fix is a generic primitive (the model reasons; the tool exposes the primitive) — never a
  magic constant. This is enforced executably by the construction guardrails (G1–G3) and is
  the whole premise of the project. If *zu* can't do something, that's a capability gap to fix
  upstream, not to hack around (the `zu_report_gap` MCP tool + the `zu://contributing`
  resource turn a gap into a reproducible issue).
- **Cost is first-class, and tests are $0.** The runtime tracks tokens/$ per run
  (`cost.jsonl`). Construction and the test suite run offline against captured fixtures at
  ~$0; the live model is reserved for one capture and one canary.

## The construction sequence (how an agent is built)

`task + target site → production agent with a resilient track`, frontier spend bounded to one
live capture:

```
zu init                         # scaffold agent.yaml
zu capture <agent>              # ONE live run → fixtures/capture.json   (or: drive zu_explore
                                #   from your own harness over `zu mcp` — your discovery IS the path)
zu run <agent> --offline        # replay the captured path at ~$0; iterate the agent freely
zu build <agent>                # offline spine: build → record track → harden
zu construct <agent> --check    # the anti-hardcode readiness gate (G1–G3), $0
zu construct <agent> --sandboxed  # autonomous construction, contained (Docker)
# then one live canary, then: zu pack / zu deploy
```

It is all drivable from a coding harness (Claude Code / Cursor / Codex) over the `zu mcp`
server — `zu_scaffold/validate/run/traces`, `zu_explore/explore_save`,
`zu_offline_run/build/harden/construct`, `zu_report_gap`.

## Where things live

- Shipped example (the flagship): **`examples/agents/vet-appointment/`** — the *only* example
  agent. Test-fixture agents (machinery, not demos) live in `packages/zu-cli/tests/agents/`.
- The construction surface is in `packages/zu-cli/src/zu_cli/`:
  `offline.py` (replay + `FixtureSessionBackend`), `build.py`, `harden.py`, `guardrails.py`,
  `construct.py` (the meta-agent driver + `LiveStrategist`), `construct_sandbox.py`,
  `explore.py`, `contribute.py`, `mcp_server.py`.
- Per-package details: each `packages/zu-*/README.md`. Ports + recipes: AGENTS.md.
- **Upstream conformance** (the mechanical guarantees a credential/capability
  consumer builds on): the pre-execution gate (`InvocationGate`), idempotency,
  run-level taint, durable grant state, human-pause/resume, the hash-chained audit
  log, harness-owned `Channel`s, out-of-process plugins (`zu_core.rpc` +
  `zu_backends.oop_launcher`), `WorkloadIdentity`, and `EgressEnforcement`. Spec +
  status matrix: [`zu-upstream-conformance.md`](zu-upstream-conformance.md);
  trusted-base enumeration: [`docs/TCB.md`](docs/TCB.md); every requirement has a
  named offline proof, guarded by
  `packages/zu-core/tests/test_conformance_matrix.py`.
- Outside the package workspace: **`automation/gap-triage/`** (zu maintaining zu — the
  CI triage agent + `zu_cli.gap_triage`) and **`community/discord-bot/`** (community infra).
