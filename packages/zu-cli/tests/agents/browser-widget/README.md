# browser-widget — offline test fixture (not a shipped example)

A minimal tier-2 agent used as the **keystone test fixture** for the offline construction
machinery (it backs the `test_offline` / `test_build` / `test_harden` / `test_guardrails` /
`test_construct*` / `test_explore` suites). It drives a persistent `browser` session entirely
from a captured `fixtures/capture.json` — no model, no network, ~$0:

```sh
zu run packages/zu-cli/tests/agents/browser-widget/ --offline
```

The arc it exercises: `http_fetch` a JS shell → the `js-shell` detector escalates to tier 2 →
`browser` open → act (click to reveal) → read → return grounded JSON (`Acme Widget`, `$9.00`).
The bundle is hand-authored so the suite needs no keys.

To **build a real agent**, see the guide at `docs/agent-construction-sequence.md`; the sole
shipped example is `examples/agents/vet-appointment/`.
