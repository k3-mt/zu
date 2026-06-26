# The capability surface — what Zu provides, here

> Not to be confused with the root `CAPABILITIES.md` (the *build spec*). This doc is
> the **runtime manifest**: what Zu actually exposes and how to discover it from the
> outside. (Issue #30.)

Zu ships a lot of capability across sibling packages, but it used to be undiscoverable,
un-type-checkable, and un-version-stamped from a consumer's view — enough that a
downstream integrator reimplemented Shadow, the recognizers, and providers locally,
missing the safety those packages already carry (redaction, a promotion gate,
content-free invariants). These artifacts make the surface explicit and machine-checkable.

## Ask the install what it actually has

The live source of truth — reconciled against the packages installed in *your*
environment, not just what the core promises:

```bash
zu capabilities          # human-readable: kinds, interface majors, installed flags, impls
zu capabilities --json   # the same as JSON, for tooling
```

From a running process (self-describing — no git-pin lookup needed):

```python
import zu_core
zu_core.__version__         # e.g. "0.3.0"
zu_core.provenance()        # {"version": ..., "interface_majors": {kind: major, ...}}
zu_core.capabilities()      # [Capability(kind, interface_major, group, implementations, installed), ...]
zu_core.library_surface()   # the import-only packages (below), with an installed flag
```

Every Zu package ships a PEP 561 `py.typed` marker, so a strict-typed downstream gets
Zu's real types (no `import-untyped` errors) — the typed public symbols **are** the contract.

## Plugin kinds (`INTERFACE_VERSION`)

Discovered via entry points; `zu capabilities` marks which are installed here. All at
interface major **v1**.

| kind | entry-point group | implementing package(s) |
|---|---|---|
| `providers` | `zu.providers` | zu-providers (`scripted`, `anthropic`, `openai-compatible`) |
| `tools` | `zu.tools` | zu-tools (`web_search`, `http_fetch`, `browser`, `recall`, `action_surface`, `pointer`, `vision`, …) |
| `detectors` | `zu.detectors` | zu-checks (`bot-wall`, `captcha`, `human-gate`, `action-surface-blind`, …) |
| `validators` | `zu.validators` | zu-checks (`schema`, `grounding`) |
| `backends` | `zu.backends` | zu-backends (`local-docker`, `oop-launcher`) |
| `sinks` | `zu.sinks` | zu-backends (`sqlite`, `jsonl`) |
| `policies` | `zu.policies` | — (define your own; `zu_providers.llm_policy` is the common base) |
| `triggers` | `zu.triggers` | zu-backends (`webhook`, `queue`, `schedule`, `object-store`) |
| `gates` | `zu.gates` | — (`InvocationGate` port in `zu_core.ports`; supply your own) |
| `channels` | `zu.channels` | zu-backends (`credential-broker`) |
| `workload_identity` | `zu.workload_identity` | zu-backends (`static`) |
| `egress_enforcement` | `zu.egress_enforcement` | zu-backends (`docker-internal-net`, `nftables`) |
| `replay_arbiters` | `zu.replay_arbiters` | — (`ReplayArbiter` port; supply your own) |
| `monitors` | `zu.monitors` | — (`Monitor` port; supply your own) |
| `patterns` | `zu.patterns` | zu-patterns (`cookie_banner`, `login_form`, `search_box`, `cart_checkout`, …) |
| `credential_brokers` | `zu.credential_brokers` | — (port in `zu_core.ports`; the broker also ships under `zu.channels`) |

A "—" means no implementation is registered for that kind in a default install — the
port exists in `zu-core`, you bring the plugin. `zu capabilities` shows the real
per-environment state.

## Library packages (import directly — no entry points)

The most valuable pieces for building an agent that operates real sites, and the
easiest to miss because they aren't plugin-discovered. **Import them; don't rebuild them.**

| package | what it's for | import |
|---|---|---|
| **zu-shadow** | record a task once → synthesize a resilient path → run it live & generalise; redaction at capture (§9) + a promotion gate | `zu_shadow.Recorder`, `zu_shadow.Synthesizer`, `zu_shadow.live_executor.run_live`, `zu_shadow.verify_and_gate` |
| **zu-patterns** | recognize surface archetypes (§5), cross-run site memory as an FSM, live MPC loop | `zu_patterns.recognize`, `zu_patterns.fsm_from_events`, `zu_patterns.mpc_run` |
| **zu-providers** | model providers, incl. any OpenAI-compatible endpoint (OpenRouter, Together, vLLM, …) | `zu_providers.openai_compatible:OpenAICompatibleProvider`, `…:AnthropicProvider`, `…:ScriptedProvider` |
| **zu-checks** | detectors (bot-walls, CAPTCHAs, human gates) + validators (schema, grounding) | `zu_checks.detectors`, `zu_checks.validators` |

> The single most striking downstream miss was `zu_patterns.fsm_from_events` — the
> cross-run memory layer — assumed missing but shipped all along. It just wasn't
> discoverable. Now `zu capabilities` and this table point straight at it.
