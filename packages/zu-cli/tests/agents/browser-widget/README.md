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

## A real agent — the full sequence

This bundle is hand-authored so the example needs no keys. A real agent records its own by
driving the live site **once**, then everything downstream is offline at ~$0. There are two
ways to do that one live discovery:

```sh
# Option A — zu pathfinds with its own configured model:
zu capture examples/agents/my-agent/        # LIVE — keys + network → fixtures/capture.json

# Option B — YOUR harness model pathfinds (Claude Code / Codex / Cursor over `zu mcp`):
#   zu_explore(tool="http_fetch", url=...) → see the JS shell
#   zu_explore(tool="browser", op="open", url=...) → act → read … until you reach the data
#   zu_explore_save(agent="examples/agents/my-agent/", task=..., answer=...)
# Your discovery in the harness you already use BECOMES fixtures/capture.json.
```

Then iterate and ship — all offline, ~$0 (the model returns only on divergence at run time):

```sh
zu run       examples/agents/my-agent/ --offline   # replay; iterate the agent for free
zu build     examples/agents/my-agent/             # build → record track → harden
zu construct examples/agents/my-agent/ --check     # the anti-hardcode readiness gate (G1–G3)
zu construct examples/agents/my-agent/ --sandboxed # autonomous, contained (needs Docker)
# then one live canary, then: zu pack / zu deploy
```

That one live capture (A or B) is the only live spend of the construction sequence.

## Hit a wall zu can't pass?

If zu genuinely can't do something — a missing primitive, a detector that won't fire, a
selector it can't resolve — that's a **capability gap in zu, not a bug in your agent**.
Don't hardcode around it: call `zu_report_gap` (over `zu mcp`) to file a strong, **repeatable**
issue. Your `fixtures/capture.json` is the repro — maintainers reproduce it with
`zu run --offline` and build the generic capability. See the `zu://contributing` resource.
