# Navigation-reliability harvest — clean-room capability matrix

> **What this is.** A clean-room study of the *navigation-reliability and failsafe* behavior of
> three competing browser-automation projects, and the map from each behavior to the generic,
> event-sourced, bounded, replayable primitive zu implements natively in its tool path.
>
> **Clean-room hygiene (how this was produced).** The three competitor repos were studied
> **behavior-only**. No competitor source was cloned into this tree, and **no code was copied or
> transcribed** — `git clone` of the competitors is blocked by this environment's egress policy, so
> their public source/docs were read via `raw.githubusercontent.com` and each behavior was reduced
> to (a) what it does, (b) the underlying **public CDP / Playwright / DOM / Rust technique**, and
> (c) a citation. Every row is behavior-derived. The competitors treat the model as *trusted*; zu
> treats it as *untrusted* — so we harvest only the model-agnostic reliability plumbing
> (auto-wait, settle, bounded retry, navigation-complete detection, action-effect verification,
> gated grounding fallback), never their trust posture.

## Competitors studied

| Repo | Language | What it is | Sources read (examples) |
|---|---|---|---|
| [`browser-use/browser-use`](https://github.com/browser-use/browser-use) | Python, CDP-native | "recovery loops inspired by coding agents"; a watchdog-based session | `browser_use/browser/watchdogs/{dom,default_action,downloads,security,crash,popups,aboutblank}_watchdog.py`, `browser_use/browser/session.py`, `browser_use/dom/service.py`, `docs.browser-use.com` |
| [`browser-use/browser-harness`](https://github.com/browser-use/browser-harness) | Python, ~1k LOC raw-CDP harness | thin single-WebSocket CDP harness; agent self-improves by writing helper code | `src/browser_harness/{helpers,daemon,admin,run}.py`, `SKILL.md`, `AGENTS.md`, `interaction-skills/*.md` |
| [`vercel-labs/agent-browser`](https://github.com/vercel-labs/agent-browser) | Rust daemon + direct CDP (Apache-2.0) | accessibility snapshots + stable `@ref`s | `cli/src/native/{browser,actions,element,interaction,snapshot,diff,network}.rs`, `README.md` |

## The capability matrix

Each row is one reliability behavior: which competitors exhibit it → the underlying mechanism →
zu's status today → the native zu primitive that matches or exceeds it.

### A. Auto-wait / settle before acting

| Behavior | Competitor(s) | Mechanism (public technique) | zu today | zu primitive |
|---|---|---|---|---|
| Settle before reading/acting: probe in-flight network + `document.readyState`, pause briefly, filter junk resources, hard-cap the wait | browser-use (`dom_watchdog.py`); browser-harness (`wait_for_load`, network-idle debounce); agent-browser (`poll_network_idle`) | CDP `Network.requestWillBeSent/loadingFinished/loadingFailed` pending-set + ~500ms quiet window; `Runtime.evaluate` of `performance.getEntriesByType('resource')` + `readyState`; outer timeout cap; junk-resource filter to avoid false-never-settle | **partial** — `wait_until`/`wait_for`/`wait_ms` exist but are **model-chosen** and forwarded verbatim (`render.py`, `browser.py`); nothing is a *precondition* of acting | **SettleGate** (`feat/nav-reliability-settle-gate`) |
| Visible + hittable precondition before a click (scroll-into-view, occlusion/hit-test, reject mis-targets) | browser-use (`default_action_watchdog.py`); agent-browser (`element.rs` `blockerAt`) | CDP `DOM.scrollIntoViewIfNeeded`, `DOM.getContentQuads`/box-model, `elementFromPoint` hit-test before `Input.dispatchMouseEvent` | **partial** — `action_surface` prunes invisible/zero-area nodes and surfaces `disabled` state, but nothing asserts *visible+enabled* at act time | **SettleGate** (the pre-act arm asserts the target affordance is visible+enabled) |
| Bounded-poll-until-predicate for waits not expressible as one lifecycle event (text/url/selector/fn) | agent-browser (`actions.rs`, `AUTH_LOGIN_SELECTOR_POLL_INTERVAL_MS`) | fixed-interval `Runtime.evaluate` predicate poll under an overall timeout | n/a (model loops by hand) | folded into **SettleGate** (generic bounded poll) |

### B. Navigation-complete / redirect / SPA-settled detection

| Behavior | Competitor(s) | Mechanism | zu today | zu primitive |
|---|---|---|---|---|
| Tiered, navigation-correlated lifecycle wait (commit < domcontentloaded < load < networkidle); stale events from a prior nav can't satisfy it | browser-use (`session.py`/`events.py`, `loaderId`-correlated; dual nav/lifecycle timeouts) | CDP `Page` lifecycle events polled, correlated to the current `loaderId`/navigation id | **partial** — `wait_until=networkidle` is the nav-done signal but model-opt-in per call | **SettleGate** (post-act arm) |
| Same-document (pushState/hash) nav skips the load wait so it doesn't hang | agent-browser (`browser.rs` `loader_id` branch) | a missing *new* `loaderId` ⇒ same-document route change ⇒ skip lifecycle wait | n/a | **SettleGate** (two equal surface fingerprints short-circuit a no-op nav) |
| Default to an **always-terminating** signal (Load, not NetworkIdle) so a long-poll/websocket page can't stall forever | agent-browser (`actions.rs` rationale comment) | `Page.loadEventFired` default; NetworkIdle opt-in | n/a | **SettleGate** default policy (critical for an *untrusted* page: a hostile/buggy surface must never stall the runtime) |

### C. Bounded retry-on-stale / transient recovery

| Behavior | Competitor(s) | Mechanism | zu today | zu primitive |
|---|---|---|---|---|
| Stale element/ref recovery: re-resolve the SAME logical control by role+name+nth occurrence, re-dispatch, bounded | agent-browser (`element.rs` `@ref` fast-path → re-query AX tree, role+name+nth re-match); browser-use (`default_action_watchdog.py` stale-node recovery; `consecutive_failures` cap=5) | opaque ref → live `backendNodeId` indirection; on `DOM.resolveNode` miss, re-run `Accessibility.getFullAXTree` and re-match identity; bounded re-resolution | **absent in the live path** — `pointer`/`action_surface` return `{stale_handle}` (a *signal*, not a retry); the only bounded soft-miss retry (`_REPLAY_MAX_SOFT_MISSES=3`) is **replay-only** | **StaleHandleRetry** (`feat/nav-reliability-stale-retry`) |
| Transient transport-error retry (distinguish retryable from real) | agent-browser (README: EAGAIN); browser-harness (stale-session single re-attach + replay-once) | bounded re-issue on a recognised transient error class | n/a (single dispatch) | folded into **StaleHandleRetry** budget |

> The `@ref → backendNodeId` indirection in agent-browser is **structurally identical to zu's opaque
> `handle_map`**: the model holds a stable token, the runtime owns the live binding and may silently
> re-bind it. zu adopts the same role+name+nth re-resolution as a generic primitive **without ever
> exposing a selector** to the untrusted model (the §11.3 confused-deputy invariant).

### D. Action-effect verification (did the act change the surface?)

| Behavior | Competitor(s) | Mechanism | zu today | zu primitive |
|---|---|---|---|---|
| Read back the property that *should* have changed (checked/value/scroll position); empty/identical diff ⇒ silent no-op | agent-browser (`interaction.rs` check/uncheck read-back oracle; `diff.rs` Myers AX-snapshot diff, identical short-circuit); browser-use (typed-input read-back); browser-harness (post-scroll re-read; before/after screenshot) | post-action state read as a success oracle; serialized before/after surface diff | **absent in zu** — exists only **downstream** in conduit (`conduit_api/effect.py` `verify_effect`), explicitly a "robust stand-in" | **EffectVerify** (`feat/nav-reliability-effect-verify`) — generalize `verify_effect` **UP** into zu so every consumer inherits it |
| A surface fingerprint that folds affordance **states+values** (so a state-only change — radio checked, swatch selected — is detectable) | agent-browser (serialized AX-tree snapshot); browser-use (`element.value`/`checked` read-back) | a digest folding per-affordance role+name+states+value | **partial** — `surface_state_id` is coarse (url+title or sorted handles → it ignores states/values); `ContentView.hash()` is content not affordance-state; `SurfaceView` has **no** `fingerprint()` | `SurfaceView.fingerprint()` (shipped with **EffectVerify**) |

### E. Gated grounding fallback when the a11y tree misses an element

| Behavior | Competitor(s) | Mechanism | zu today | zu primitive |
|---|---|---|---|---|
| When the a11y tree under-reports, a secondary deterministic DOM heuristic synthesizes the actionable node and exposes it via the same opaque handle (never a raw selector) | agent-browser (`snapshot.rs` cursor:pointer/onclick/contenteditable scan, hidden-control promotion, same `ref_map`); browser-use (`dom/service.py` `backendDOMNodeId`→DOMSnapshot bounds + `getEventListeners`, cost-capped); browser-harness (compositor coordinate click, structured-first-then-coordinate) | secondary heuristic merged into the same `@ref`/handle channel; gated/cost-capped escalation | **present** — `reduce_surface` sets `blind`/`blind_reason`; `action-surface-blind` detector → deterministic ESCALATE to vision (or human if vision is blind) | **(extended)** make the gated ladder reachable **also** from a stale-handle/no-op signal (in **StaleHandleRetry**); no standalone branch |

### F. Bounded budget for the reliability ops themselves

| Behavior | Competitor(s) | Mechanism | zu today | zu primitive |
|---|---|---|---|---|
| Every blocking reliability op carries its own small bound (settle ≤ N ms, retry ≤ K), chosen below the transport ceiling so a timeout is *reportable*, not a dropped connection or a burned global budget | agent-browser (per-command `--timeout`/`AGENT_BROWSER_DEFAULT_TIMEOUT` ~25s, kept < the 30s read timeout); browser-use (per-op mouse-down/up timeouts; `consecutive_failures` cap) | small per-op bound + hard-capped retry count | **partial** — `Budget` has `wall_time_s`/`max_tokens`/`max_steps`/`max_tool_calls`, and every tool call is wall-time-bounded via `asyncio.wait_for`, but a model-chosen `wait_ms` is unbounded except by the global wall clock | `Budget.settle_ms_max` / `Budget.stale_retries_max` (**ReliabilityBudget**, shipped with SettleGate) |

## Anti-zu — harvested as *insight only*, never implemented

These behaviors are explicitly **not** built. They depend on treating the model as trusted; zu does
not. We keep only the engineering insight.

| Anti-pattern | Repo | Why it's anti-zu | The insight we keep |
|---|---|---|---|
| Agent **writes & runs** missing helper code at runtime (`agent_helpers.py`), persists site flows to `domain-skills/` | browser-harness (`SKILL.md`, `agent-workspace/agent_helpers.py`) | free-form code execution by the untrusted model is exactly what zu's tier/gate model forbids | a recurring failure should become a **generic, vetted primitive owned by the trusted runtime** — which is precisely what `zu_report_gap` + these three primitives do |
| Self-heal by **model re-planning** after N stalls ("recovery loops inspired by coding agents") | browser-use (`agent/service.py`, release notes) | the *recovery decision* is model-driven; zu keeps recovery deterministic and harness-owned | the **bounded failure budget** + "adapt, don't repeat" discipline → `Budget.stale_retries_max` and the deterministic escalate-on-exhaustion ladder |

Adjacent (auto-dismiss JS dialogs, crash/liveness heartbeat, WS reconnect) lives in the container
browser server, not zu's tier model; zu's `asyncio.wait_for` wall-time wrap already prevents an
unbounded wedge at the zu boundary, so these are **out of scope** for this navigation-reliability
layer and noted here only for completeness.

## zu baseline (independently verified) → the three primitives

| # | Capability | Status | Native primitive (branch) |
|---|---|---|---|
| 1 | Auto-settle before acting (DOM-stable / network-idle / visible+enabled) | partial | **SettleGate** — `feat/nav-reliability-settle-gate` |
| 2 | Bounded retry-on-stale in the **live** (non-replay) path | absent | **StaleHandleRetry** — `feat/nav-reliability-stale-retry` |
| 3 | Navigation / SPA-settled detection in the live path | partial | **SettleGate** (post-act arm) |
| 4 | Native content-free action-effect / silent-no-op verification | absent (conduit-only) | **EffectVerify** — `feat/nav-reliability-effect-verify` |
| 5 | Gated grounding fallback when a11y misses an element | present | extended (reachable from stale-exhaustion) |
| 6 | Event-sourcing of reliability actions | present (seam ready) | new `data.effect.verified` / `data.settle.waited` / `data.handle.rebound` |
| 7 | Budget-bounding of waits/retries | partial | **ReliabilityBudget** (`settle_ms_max`, `stale_retries_max`) |
| 8 | A `SurfaceView` fingerprint folding affordance states+values | partial | `SurfaceView.fingerprint()` |

### Build order (each: native implementation + offline ScriptedProvider/fake-session test, `$0`, no Docker/network)

1. **EffectVerify** (`feat/nav-reliability-effect-verify`) — the explicit "generalize conduit's
   `verify_effect` UP into zu" ask. Lowest-risk, highest-leverage: pure functions over the frozen
   `SurfaceView`. Adds `SurfaceView.fingerprint()` (the dependency the other two reuse) and is the
   success oracle settle/stale lean on. Conduit's `effect.py` becomes a thin shim — proving the
   up-generalization.
2. **SettleGate** (`feat/nav-reliability-settle-gate`) — harness-owned, budget-bounded
   settle-before-act + post-act surface-stabilized gate; `ReliabilityBudget`. Needs (1)'s fingerprint.
3. **StaleHandleRetry** (`feat/nav-reliability-stale-retry`) — bounded live re-resolve-by-identity +
   re-dispatch, escalating into the existing gated grounding/vision ladder on exhaustion. Composes
   (1) and (2).

### zu non-negotiables every primitive honors

Generic (no site hardcoding, G1–G3) · event-sourced/auditable · **budget-bounded** (no unbounded
wait/retry) · deterministic & **replayable offline** (`ScriptedProvider` + fake session, no live
model/network/Docker) · inside the existing **tier/gate** model · the model only ever holds an
**opaque handle**, never a selector.
