# zu-detectors

Detectors — the **`Detector`** port: a judgment about an observation that returns
a `Verdict` (`WARN` / `RETRY` / `ESCALATE` / `TERMINAL`) or `None`. Detectors are
the checkpoints where the loop decides to escalate a tier, retry, or end a run —
**the model signals an action; it never acquires a capability itself.**

## Registered plugins (`zu.detectors`)

| Name | Class | Typical verdict |
|------|-------|-----------------|
| `empty` | `EmptyDetector` | `ESCALATE` — the page came back empty / blank. |
| `error` | `ErrorDetector` | `TERMINAL` — a hard error status (e.g. a 4xx/5xx). |
| `js-shell` | `JsShellDetector` | `ESCALATE` — the page is a JavaScript shell (an empty mount point), so tier 1 can't read it; climb to the browser. |
| `bot-wall` | `BotWallDetector` | `ESCALATE`/`TERMINAL` — a bot wall / captcha interstitial. |

A detector declares a `scope` (`PER_OBSERVATION`, `PER_TURN`, or `ON_FINAL`); the
loop runs it at the matching checkpoint and acts on the *worst* verdict.

## Extend

Implement the `Detector` shape (`name`, `scope`, `inspect(ctx) -> Verdict |
None`), register under `zu.detectors`, and add a test asserting the verdict on a
fixtured observation. Detector heuristics read page content via the shared
content-key helper in `__init__.py` — reuse it rather than re-deriving "what
counts as page content."

## Tests

`uv run pytest packages/zu-detectors` — offline.
