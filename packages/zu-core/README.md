# zu-core

The small, stable core of Zu: the typed contracts, the six ports, the plugin
registry, the interpreter loop, and the event bus. **It depends only on the
standard library and Pydantic** — it physically cannot import a model SDK, a
browser, or any concrete adapter. It should be readable in an afternoon.

## What's inside

| Module | Responsibility |
|--------|----------------|
| `contracts.py` | `TaskSpec`, `Result`, and the frozen `Event` envelope (types namespaced `harness.*` / `data.*`). |
| `ports.py` | The six `Protocol` ports + the capability envelope (`CAP_*`, `EGRESS_OPEN`, `declared_envelope`). |
| `registry.py` | The one registry the loop reads (entry points, decorators, config). |
| `loop.py` | The interpreter loop: provider → tool → detectors → finalise → validators, with the escalation ladder and budgets. |
| `bus.py` | The event bus: append-before-notify, with isolated destinations. |
| `events.py` | The event taxonomy (the stable set of `harness.*` / `data.*` type constants). |
| `sinks.py` | The in-memory default `EventSink`. |
| `eventstore.py` / `codec.py` / `projections.py` | Shared filter contract, the encryption-at-rest seam, and rebuildable read-side views. |

## The six ports

`ModelProvider`, `Tool`, `Detector`, `Validator`, `SandboxBackend`, `EventSink`
— each a runtime-checkable structural `Protocol`. A plugin implements the
*shape*; it never subclasses a framework.

This package registers **no plugins** — it defines the contracts every other
package plugs into.

## Tests

`uv run pytest packages/zu-core` — deterministic, offline.
