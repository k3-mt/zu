# browser-widget — the offline tier-2 keystone example

A minimal agent that drives a **persistent `browser` session** to read data a
JavaScript widget renders — and runs **fully offline**, no model and no network, at
**~$0**:

```sh
zu run examples/agents/browser-widget/ --offline
```

You should see `status : success`, a `track.json` written next to the agent, and a
`cost :` line at ~`$0` (zero model tokens — the run replays captured moves, not a model).

## How it works

`fixtures/capture.json` is the captured bundle the offline run replays:

- **`moves`** — the model's decisions, replayed by a `ScriptedProvider`: `http_fetch`
  the page (a JS shell with no data) → the `js-shell` detector escalates to tier 2 →
  `browser` `open` → `act` (click to reveal) → `read` → return the grounded JSON.
- **`observations`** — what each tool returned, replayed in order by the fixture
  doubles: the `http_fetch` shell, then the three `browser` session responses. The
  final answer (`Acme Widget`, `$9.00`) appears verbatim in the last `browser` read, so
  the `grounding` validator passes.

## A real agent

This bundle is hand-authored so the example needs no keys. A real agent records its own
by driving the live site **once**:

```sh
zu capture examples/agents/my-agent/      # LIVE — needs keys + network, writes fixtures/capture.json
zu run    examples/agents/my-agent/ --offline   # then iterate offline, ~$0
```

That one live capture is the only live spend of the construction sequence — everything
after it (build, record the track, harden against perturbed fixtures) runs offline.
