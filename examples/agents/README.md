# Example agent

One real, runnable agent — the flagship. `agent.yaml` *is* the whole agent (task + model +
the tier ladder of tools), and the repo's test suite proves it works offline (real tools +
validators, a scripted model — no key, no network).

## [`vet-appointment/`](vet-appointment/) — the flagship

Autonomously drives a real, multi-step JavaScript booking widget (Park Vets / Vetstoria) and
returns the **3 soonest available appointment slots** — every slot grounded against the page,
nothing invented. It searches the web for the booking page, fetches it, then escalates to a
**persistent browser** to work the wizard (location → new client → species → appointment type
→ calendar) and read the times.

```bash
export EXA_API_KEY=...           # web_search (Exa)
export OPENROUTER_API_KEY=...    # the model (OpenRouter)
zu run examples/agents/vet-appointment/              # live
zu run examples/agents/vet-appointment/ --sandboxed  # contained, behind the egress proxy
```

It ships a recorded `track.json`, so re-runs **replay the path deterministically** — the model
returns only at the frontier (the final extraction). Proven economics: **~$2.17 to pathfind
live → ~$0.03/run on replay** (a cheap finisher), ≈75× cheaper.

## Build your own

`zu init` scaffolds a starter `agent.yaml`. The cheap path from "a task + a target site" to a
production agent — discover once, then iterate **offline at ~$0**, harden, and ship — is the
**[Building an agent guide](../../docs/agent-construction-sequence.md)**. You can drive the whole
sequence from your own coding harness (Claude Code, Cursor, Codex) over `zu mcp` — see
[`../integrations/`](../integrations/).

## How it's tested
- **unit/integration lane** — `packages/zu-cli/tests/` runs agents offline against saved
  fixtures (real `http_fetch`/`html_parse` + `schema`/`grounding`, a scripted model).
- **docker lane** — `validation/containment/` runs the whole agent inside the hardened
  container behind the egress proxy, surfacing the in-container event log across the boundary.
