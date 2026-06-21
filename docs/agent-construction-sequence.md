# Building agents cheaply: the construction sequence

How to go from **"a task + a target site" → "a production agent with a resilient
track"** repeatably, with the **frontier spend bounded to a single live capture**. This
doc designs the end-to-end sequence, names what this PR builds versus what it specs
behind seams, and lays out the trade-offs.

The discipline is unchanged: **generic capabilities (no site-specific hardcoding),
empirically verified, grounded output, cost tracked as first-class.**

## The problem: where the money goes today

The current loop: hand-author `agent.yaml` → `zu run --no-track` live with a frontier
model → it pathfinds, hits a wall → a human probes and builds a generic capability
(rebuild the browser image if the primitive changed) → repeat to success → `track.json`
records → harden the replay.

The cost lives in four places, and the first dominates:

| Driver | Why it costs | Today |
| --- | --- | --- |
| **Frontier $ re-paid every iteration** | No track exists *during* construction, so each loop re-drives the solved prefix LIVE | ~$2.17/run, ×N iterations |
| **Live-site latency & nondeterminism** | Every iteration hits the real site; flaky, slow, rate-limited | wall-clock + retries |
| **Docker rebuild inner loop** | A change to the browser primitive means rebuild the tier-2 image | minutes/iteration |
| **Serial human diagnosis** | A human reads the log at each wall and decides the next capability | human time |

The lever: **the solved prefix should be free to re-drive.** A track makes replay free —
but only *after* success. During construction there is no track, so the prefix is paid
for, live, every time. Close that and the inner loop becomes free.

## The sequence

```
 1 Scaffold       zu init             EXISTS   scaffold.py
 2 Capture (live) zu capture          BUILT    ← the ONLY live spend; event log → fixtures/
 3 Build offline  zu run --offline    BUILT    ← THE KEYSTONE; ~$0/iter, deterministic
 4 Record track   track.json          EXISTS   record_track / loop._replay_track
 5 Harden (chaos) resilience score    SPEC     reuse zu-redteam verdict-observer
 6 Live canary    one live run        SPEC     assemble from the existing run path
 7 Promote        zu pack / zu deploy EXISTS   deploy.py
   META-AGENT     --sandboxed + zu mcp loop   SPEC + skeleton — cheap ONLY because it wraps stage 3
```

Stage 3 is the keystone: it turns the construction inner loop (diagnose a wall → edit a
capability or `agent.yaml` → re-run) from *frontier-priced and live* into *free and
deterministic*. Everything cheap downstream — iterating, hardening, and the meta-agent —
depends on it existing.

### Stage 2 — `zu capture` (the one live step)

Drive the target **once**, live, and project the run's event log + result into
`fixtures/capture.json`: the model's `moves` (ordered, one per tool call, then the final
answer) and each tool's `observations` (ordered). It mirrors `record_track`
(`zu_core/track.py`) — same `harness.tool.invoked` events for the moves, the paired
`harness.tool.returned` events for the observations. This is the only step that needs
keys and network.

### Stage 3 — `zu run --offline` (the keystone)

Replace the live model with a `ScriptedProvider` of the captured moves, and rebind the
off-box tools to fixture doubles through their **existing injection seams**:

- `http_fetch` → `HttpFetch(transport=httpx.MockTransport(...))`
- `render_dom` → `RenderDom(backend=FixtureRenderBackend(...))`
- `browser` → `Browser(backend=FixtureSessionBackend(...))` **← the new seam**

`render_dom` and `http_fetch` already had offline seams (see `_escalation_registry` in
`zu_cli/demo.py`). The persistent `browser` session did **not** — that was the gap.
`FixtureSessionBackend` (in `zu_cli/offline.py`) closes it: it implements
`SessionBackend`/`BrowserSessionHandle` (`zu_core/ports.py`), replaying an ordered
observation sequence, **faithful to the loop's soft-miss handling** (a recorded
`action_error_kind: "soft"` replays as a soft miss, not a challenge — see
`loop._is_soft_miss`) and **loud on overrun** (a fixture that runs short returns an error
observation so the run fails as a challenge, never a silent pass).

Detectors, validators, and the event sink stay real, so the loop, track recording
(stage 4), and cost telemetry are exercised exactly as live — and `cost.jsonl` proves
**0 model tokens / ~$0 per iteration**.

### Stages 5–7

- **Harden (5)** — perturb the fixtures (rename/drop selectors, inject a consent banner,
  reorder steps, AB-swap variants), replay `track.json` against each variant, score the
  pass rate, and flag brittle single-selector steps. *Specced below.*
- **Canary (6)** — one live validation run before promotion, guarding fixture drift.
  *Specced: assemble from the existing live `run` path.*
- **Promote (7)** — `zu pack` / `zu deploy` → a production bundle + the resilient track.
  *Exists (`zu_cli/deploy.py`).*

## The four candidate processes (and why the pipeline composes them)

| | Candidate | What it buys | Cost | Reliability | Complexity | HITL |
| --- | --- | --- | --- | --- | --- | --- |
| **A** | **Offline fixtures (the keystone)** | Free, deterministic inner loop | ~$0/iter after 1 capture | High *for the captured path*; blind to drift | Low — reuses existing seams + one new backend | Human reads logs, edits capabilities |
| **B** | **Scout↔frontier builder-tiering** | Cheap model does tier-1 legwork; frontier only on escalation | Lower $/live-run | Same as the model mix | Low — reuses the loop's per-tier `providers` override | — |
| **C** | **Chaos hardening** | A resilience score; surfaces brittle steps before prod | Free (runs on fixtures) | Raises the floor on drift | Medium — perturbation + scoring | Human reviews the score |
| **D** | **Meta-agent wrapper** | Automates diagnosis + the edit loop | Token cost of the builder | Needs guardrails or it games the gate | High — autonomous loop in a sandbox | Review gate on the output bundle |

The recommended pipeline composes all four: **A** makes the loop free, **B** trims the
live capture and canary, **C** turns "it replayed once" into "it survives drift", and
**D** automates the human-in-the-loop — but **D is cheap only because it wraps A.** A
meta-agent that iterated live would re-pay the frontier prefix every loop; iterating
against fixtures is what makes its token budget tractable. Hence A lands before D.

## New vs reused

**Built here (the gap):**

- `zu_cli/offline.py` — the bundle format + loader, `FixtureSessionBackend` (new seam),
  `FixtureRenderBackend`, the `http_fetch` mock-transport double, `rebind_offline`, and
  `project_capture`.
- `zu run --offline` wired through `_execute_once`; the `zu capture` command.
- `examples/agents/browser-widget/` — a minimal tier-2 example that runs fully offline.
- `packages/zu-cli/tests/test_offline.py`.

**Reused (cited):** `zu_core/loop.py` (`_replay_track`, `_is_challenge`/`_is_soft_miss`,
escalation), `zu_core/track.py` (`record_track`), `zu_core/cost.py` (`summarize_cost`,
`cost.jsonl`), `zu_core/ports.py` (`SessionBackend`/`BrowserSessionHandle`),
`zu_tools/{fetch,render,browser}.py` (the injectable seams), `zu_providers/scripted.py`
(`ScriptedProvider`), `zu_cli/demo.py` (the offline-registry pattern),
`zu_cli/config.py` (`assemble`/`build_registry`, the per-tier `providers` override),
`zu_cli/{deploy,scaffold,mcp_server}.py`.

## Cheapest validation ($0, no live spend)

`packages/zu-cli/tests/test_offline.py` validates the machinery offline:

- the `browser-widget` example runs through `FixtureSessionBackend` to a grounded
  SUCCESS at **0 model tokens** (`uv run zu run examples/agents/browser-widget/
  --offline`);
- a short browser fixture fails **loudly** (overrun → challenge, not a silent pass);
- a recorded soft miss replays as a soft miss;
- `project_capture` round-trips a synthetic event log → bundle → reproduced result.

**Out of scope here (each needs one live run — keys + network):** a real `zu capture`,
the stage-6 canary, and a real meta-agent build. `zu.db` is empty locally; do not
hand-fake a captured bundle for a real site and claim it passes.

## Specced behind seams

### Stage 5 — chaos hardening (reuse the verdict-observer pattern)

Model it on `zu-redteam`'s out-of-band gate (`packages/zu-redteam/src/zu_redteam/
verdict.py`): a `VerdictObserver.inspect(run) -> Breach | None` panel scored by
`render_verdict`. The hardening analogue:

- `perturb_bundle(bundle) -> list[Bundle]` — generate variants: rename/drop a selector
  in a `browser` `act`, inject a consent banner observation before a step, reorder two
  independent steps, swap an AB variant of the page text.
- Replay `track.json` against each variant offline (stage 3 machinery); a `ResilienceObserver`
  reads each `ObservedRun` and scores pass/fail; the **resilience score** is the pass
  rate, with brittle single-selector steps named.
- Surface as `zu harden`, gating promotion on a threshold.

Seam: this is pure offline replay over the same `capture.json` format — no new live
dependency.

### The meta-agent / container-CLI builder

A Claude CLI running autonomously **inside `zu run --sandboxed`** (the hardened container
+ egress proxy: `main._execute_sandboxed` + `SandboxLauncher`), driving the **existing
`zu mcp` tools** (`zu_scaffold` / `zu_validate` / `zu_run` / `zu_traces`,
`zu_cli/mcp_server.py`):

1. **Capture once live**, then iterate stages 3–5 **offline and free** — explore,
   diagnose, and harden without re-paying the frontier prefix.
2. **Read its own event logs** (`zu_traces`) to diagnose each wall and decide the next
   capability or `agent.yaml` edit — the loop the human does today, automated.
3. **Hand back a bundle for review** (`agent.yaml` + hardened `track.json` + `fixtures/`)
   with a cost ledger showing construction spend.

**Anti-hardcode guardrails (load-bearing, not optional)** — without these a meta-agent
can "click Chislehurst" and pass the gate by memorising the answer:

- a capability that targets an element must carry **≥1 alternate selector** (no
  single-point-of-failure step);
- the track must clear a **stage-5 resilience threshold** (it survives perturbed
  fixtures, not just the captured one);
- the builder emits **generic capabilities only** — never a literal site-answer constant
  baked into config or a tool;
- the output is **gated by review** (human or automated) before promote.

A thin orchestration skeleton may land as `zu_cli/build.py`, chaining the **offline**
stages (3–5) with the live `capture`/`canary` and the autonomous loop behind explicit
`NotImplementedError` seams — so the cheap, testable spine exists and the live/autonomous
parts are clearly marked as the next increment.

## Risks / assumptions to challenge

- **Fixtures ≠ the live site.** Offline-green is not live-green under drift — the
  stage-6 canary is the guard.
- **A meta-agent could hardcode "to pass."** The anti-hardcode guardrails + the stage-5
  chaos score + the review gate are load-bearing.
- **Meta-agent token cost could exceed the frontier savings** — unless it iterates
  offline. Hence A before D.
- **Is navigation or human diagnosis the real cost?** Use `cost.jsonl` to confirm which
  dominates before over-investing in D.
- **Track resilience may not generalize** across genuinely different DOMs — the chaos
  score measures *this* track against *these* perturbations, not all futures.
