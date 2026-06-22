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
 5 Harden (chaos) zu harden           BUILT    offline brittleness audit + resilience score
 6 Live canary    one live run        SPEC     assemble from the existing run path
 7 Promote        zu pack / zu deploy EXISTS   deploy.py
   ─────────────────────────────────────────
   SPINE         zu build            BUILT    chains stages 3→4→5 offline, gated, at $0
   GUARDRAILS    zu construct --check BUILT   executable anti-hardcode gate (G1–G3)
   META-AGENT    zu construct + loop  BUILT*   driver loop + gate built; live brain is a seam
```

`zu build` is the offline spine: it runs stages 3→4→5 in order (build → record track →
harden), gating the recorded track on the resilience score, and writes a hardened
`track.json` at $0. The live stages (2 capture, 6 canary) and promotion (7) sit outside
it, behind seams. Stage 3 is the keystone: it turns the construction inner loop (diagnose a wall → edit a
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

- **Harden (5)** — `zu harden` (in `zu_cli/harden.py`). *Built, offline-$0:* a static
  brittleness audit names single points of failure (single-selector steps, single-
  occurrence grounded values), and perturbation replay scores resilience — cosmetic page
  noise the path should absorb, with value-deletion variants as a control that must fail
  (proving grounding gates). *What remains (live lane):* adaptive-recovery hardening
  (re-pathfinding around a renamed selector or an injected interstitial) needs a live
  model and is the next increment — see below.
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
  `FixtureRenderBackend`, the `http_fetch` mock-transport double, `rebind_offline`,
  `project_capture`, and the reusable `replay_offline`.
- `zu_cli/harden.py` — the stage-5 brittleness audit + perturbation-replay resilience
  score; the `zu harden` command.
- `zu_cli/build.py` — the offline spine (`build_offline`) chaining stages 3→4→5; the
  `zu build` command, with the live canary behind a `NotImplementedError` seam.
- `zu_cli/guardrails.py` — the executable anti-hardcode gate (`enforce_guardrails`, G1–G3).
- `zu_cli/construct.py` — the meta-agent driver loop (`construct` + `Strategist` /
  `ScriptedStrategist`), with the live strategist + live capture as seams; `zu construct`.
- `zu run --offline` wired through `_execute_once`; the `zu capture` command.
- `examples/agents/browser-widget/` — a minimal tier-2 example that runs fully offline.
- `packages/zu-cli/tests/{test_offline,test_harden}.py`.

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
- `project_capture` round-trips a synthetic event log → bundle → reproduced result;
- `zu harden` audits the captured path, scores 100% resilience on cosmetic noise, and
  fails every value-deletion control (`test_harden.py`);
- the guardrail gate bites and clears (G1/G2/G3, `test_guardrails.py`) and the construction
  loop converges with a scripted strategist (`test_construct.py`).

**Out of scope here (each needs one live run — keys + network):** a real `zu capture`,
the stage-6 canary, and a real meta-agent build. `zu.db` is empty locally; do not
hand-fake a captured bundle for a real site and claim it passes.

## Stage 5 — chaos hardening (built; `zu_cli/harden.py`)

`zu harden <agent>` reports two honest, $0 signals over the captured bundle, modelled on
`zu-redteam`'s out-of-band verdict pattern (inspect a finished run from outside its trust
boundary, `packages/zu-redteam/src/zu_redteam/verdict.py`):

- **Static brittleness audit** (`audit_brittleness`) — no run required: flags
  single-selector steps (a `click`/`fill`/`select` with no `near` fallback) and
  single-occurrence grounded values (present in exactly one fixture observation, so one
  wording change loses them).
- **Perturbation replay** (`perturb_variants` + `replay_offline`) — generate variant
  bundles that keep the observation sequence aligned and vary only the page text, then
  re-run them through the offline keystone. The **resilience score** is the fraction of
  *value-preserving* (cosmetic-noise) variants the path still succeeds on;
  *value-corrupting* variants are a control that **must** fail, so the score reflects
  real grounding, not a rubber stamp. `--min-score` gates promotion.

**Honest boundary:** the replay drives the *captured* moves — a frozen model — so it
measures the path's tolerance to cosmetic drift, **not** adaptive recovery. A
perturbation that needs a new decision (re-pathfind around a renamed selector, dismiss an
unexpected interstitial) cannot be absorbed offline; measuring that needs a live model
and is the next increment (a live hardening lane), deliberately out of this $0 scope
rather than silently conflated with what is measured.

## The meta-agent — gate + driver built; only the live brain remains

The headline is a Claude CLI running autonomously **inside `zu run --sandboxed`** (the
hardened container + egress proxy: `main._execute_sandboxed` + `SandboxLauncher`), driving
the **existing `zu mcp` tools** (`zu_scaffold` / `zu_validate` / `zu_run` / `zu_traces`,
`zu_cli/mcp_server.py`) to capture once, then iterate stages 3–5 offline and free, reading
its own `zu_traces` to diagnose each wall and decide the next edit.

Two load-bearing parts are now **built** and tested at $0; only the live model brain and
live capture remain as seams.

### The anti-hardcode guardrail gate (built; `zu_cli/guardrails.py`)

`enforce_guardrails(spec, cfg, bundle, agent_dir)` makes the load-bearing rules
executable, reusing the stage-5 machinery — without these a meta-agent can "click
Chislehurst" and pass by memorising the answer:

- **G1 — alternate locators**: every targeting step carries a `near` fallback (a
  `single-selector` finding from `audit_brittleness` is a violation).
- **G2 — resilient track**: clears a resilience threshold AND grounding is load-bearing
  (reuses `harden`).
- **G3 — no hardcoded answer**: no captured grounded value (`grounded_values`) appears
  verbatim in `agent.yaml` or a bundle tool's source.
- **G4 — review gate**: structural — `construct` hands back a bundle + report for sign-off
  and never auto-promotes.

The gate is intentionally **stricter than `zu build`**: `zu build` *notes* single-selector
brittleness (the hand-authored `browser-widget` example legitimately has one); the
guardrails *fail* on it, because they gate autonomous output bound for production.

### The construction driver loop (built; `zu_cli/construct.py`)

`construct(spec, cfg, agent_dir, bundle, strategist, …)` runs the diagnose → edit →
rebuild loop: each round `build_offline` (the spine) then `enforce_guardrails` (the gate);
on a hold, ask the `Strategist` for an `Edit` (a mutated bundle) and retry. It reuses the
offline spine and the gate — no new offline machinery — and never promotes (G4).

`ScriptedStrategist` drives it deterministically (the tests prove convergence: the brittle
example + one edit that adds a `near` fallback → builds clean and clears the gate in two
rounds, no model). `zu construct --check` runs one round as a **$0 readiness gate**.

**What remains (the live brain):** `LiveStrategist.propose` (a model — the Claude CLI on
the `zu mcp` tools in the sandbox — deciding the next edit) and `live_capture` (stage 2)
are explicit `NotImplementedError` seams. The autonomous loop is cheap *because* it wraps
the offline spine; only its decision-maker needs frontier tokens. Plus the live canary
seam (`build._canary`).

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
