# zu-redteam

The plugin-test **gate** and the **adversarial red team** — the machinery behind
[`docs/PHILOSOPHY.md`](../../docs/PHILOSOPHY.md) §3 and
[`docs/RED_TEAM.md`](../../docs/RED_TEAM.md). The red team is itself a Zu agent:
Zu is the runtime on both sides of the gate.

This is test/CI infrastructure — it is **not** loaded by a deployed agent. Run it
with `zu test-plugin <pkg>` (install via `pip install 'zu-runtime[test]'`).

## What it does

A plugin is not "done" when its unit tests pass — it is done when it cooperates
with other plugins and withstands an adversary inside a real Zu runtime. The gate
runs the graded gates in order and renders one verdict:

```
zu test-plugin zu-tools
  ✅ unit         PASS
  ✅ contract     PASS — port shape + declared capability envelope
  ✅ interop      PASS — stood up with >= 3 cross-category neighbours
  ✅ adversarial  PASS — frozen corpus + directed probes; envelope held
  ⊘ container    SKIP — Docker not present (production form of the same run)
```

## The pieces

| Module | Role |
|--------|------|
| `verdict.py` | The out-of-band, deterministic **judge**: egress / exfil / provenance / resources / neighbour-health observers. The attacker never certifies. |
| `corpus.py` | The frozen **regression corpus** — the §4 attacks as deterministic runs. Only ever grows. |
| `attacker.py` | The **attacker agent** + tools + fleet. `ScriptedAttacker` (deterministic, CI); `LiveAttacker` (opt-in frontier discovery, `ZU_REDTEAM_LIVE=1`). |
| `harness.py` | Stands a target up in a real in-process Zu run and captures it for the observers. |
| `contract.py` | Port/contract conformance (shape, types, declared envelope). |
| `gate.py` | Orchestrates the gates → `GateReport`; the entry point `zu test-plugin` calls. |

## Determinism

Discovery (a live frontier attacker) is non-deterministic by design; a discovered
breach is frozen into `corpus.py` and replayed deterministically thereafter — so
CI stays reproducible while the corpus only grows. The container gate is the
production form of the same in-process run (same observers, same verdict).

## Tests

`uv run pytest packages/zu-redteam` — offline, deterministic. The suite proves the
gate both **passes** a safe plugin and **fails** an unsafe one (a tool that
under-declares egress, or leaks a planted secret).
