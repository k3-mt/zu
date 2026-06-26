# zu-shadow — author an agent by demonstration (§2.8)

> **Import these** (so you never reimplement Shadow by hand):
> | import | does |
> |---|---|
> | `from zu_shadow import Recorder` | fold a human session into `data.shadow.*` events, **redacted at capture** |
> | `from zu_shadow import Synthesizer` | turn a recording into a production agent + rail (offline, `ScriptedProvider`) |
> | `from zu_shadow.live_executor import run_live` | re-run the demonstrated path on the live site and **generalise** it |
>
> Also: `verify_and_gate` / `PromotionVerdict` (the promotion gate before a path is
> trusted), `redact_event` / `redact_text` (the §9 content-free guarantee),
> `SemanticTarget` (role+name anchors, never selectors). Run `zu capabilities` to see
> what's installed. Depending on `zu-core` alone and rebuilding this loses redaction,
> the promotion gate, and the tested live executor.

A Shadow recording **is** the event bus run over a *human* session: the human is
the policy for that one run, so recording costs almost nothing architecturally.
You drive the task once, by hand; Shadow folds your clicks, types, navigations and
the page/network metadata into `data.shadow.*` events on the same append-only log
everything else in Zu uses, then synthesizes a production agent + a rail from it.

```
record (human session) ─▶ redact-at-capture ─▶ data.shadow.* on the log
                                                     │
                              synthesize (a Zu agent, ScriptedProvider offline)
                                                     │
                       agent spec + induced Fsm + Invariants + self-writing egress
                                                     │
                      verification-replay GATE (reuses zu-cli offline.py/build.py)
                                                     │
                          promote only if the recorded outcome reproduces
```

## Four load-bearing disciplines

- **Redaction is DEFAULT-ON and runs BEFORE append** (`redaction.py`). Passwords,
  `Authorization`/`Cookie`/`Set-Cookie` headers, token/API-key shapes, and
  consumer-configured PII are stripped — *including the "why" intent text* — before
  any event reaches `EventSink.append`. The secret is gone before the event is
  hashed into the audit chain. This is conformance requirement **ZU-AUDIT-4**.
- **Capture is SEMANTIC** (`capture.py`). Every action is named by its target's
  `{role, name, label}` (the core `zu_core.surface` currency, shared with the §4
  locator and §5 `SurfaceView`) — never a CSS selector or pixel coordinate, so the
  synthesized agent re-resolves on a changed page instead of breaking.
- **The synthesizer is a Zu agent** (`synthesizer.py`). It is *driven by a*
  `ModelProvider` (offline-tested with `ScriptedProvider`). The model writes only
  the policy prompt + goal; the egress allowlist, the induced `Fsm`, and the
  `Invariant`s are **derived deterministically** from the log — the egress allowlist
  *writes itself* from the recorded `network.response` hosts. No new FSM/invariant
  types: it emits `zu_core.reachability.Fsm` and `zu_core.invariants.Invariant`.
- **Promotion is GATED by reproduced outcome** (`replay_gate.py`). A synthesized
  agent does not run on real data until it reproduces the recorded outcome, reusing
  zu-cli's `offline.py`/`build.py`. The "why" resolutions are surfaced for **review**,
  never auto-promoted.

## The honest scope

Robustness comes from the runtime machinery — semantic re-resolution, detectors,
replay, the rail — not from a single recording. On a structurally different site
the honest behaviour is to **escalate**, not silently err. The live human recorder
(real Chromium + a real human over CDP) is demo/manual, behind the `live` extra and
a manual entrypoint (`live.py`); the offline core is fully tested against a
synthetic input/CDP stream at $0.

## CLI

```
zu shadow record   <stream.json> --site <url> -o recording.json   # synthetic/live stream → recording
zu shadow synthesize <recording.json> --instruction "…"           # recording → agent + rail proposal
zu shadow scale    <agent> --rows rows.csv --var <name>           # one governed run per CSV row
```
