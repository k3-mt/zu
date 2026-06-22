# vet-appointment — the flagship agent

Find a vet practice's online booking page and return the **3 soonest available appointment
slots** as grounded JSON. A real open-web agent: it *searches*, *reads*, and drives a
multi-step JavaScript booking widget — held to its schema **and** to grounding (every returned
`date`/`time` must actually appear on the page, nothing invented). It does **not** book
anything; it surfaces the slots for a human.

```
vet-appointment/
  agent.yaml          # the whole agent: model, the tier ladder, detectors, the task
  .env.example        # copy to .env — EXA_API_KEY, OPENROUTER_API_KEY (+ OPENROUTER_BASE_URL)
  fixtures/booking.html   # a saved page, so the agent is tested offline
  track.json          # a recorded path — re-runs REPLAY it deterministically (see below)
```

## Run it

```bash
cp examples/agents/vet-appointment/.env.example examples/agents/vet-appointment/.env
# edit .env: add EXA_API_KEY (exa.ai) + OPENROUTER_API_KEY (openrouter.ai)

zu run examples/agents/vet-appointment/              # on your host
zu run examples/agents/vet-appointment/ --sandboxed  # contained on Docker
```

## How it works

- **Search → fetch → browser.** `web_search` (Exa) finds the booking page; `http_fetch` reads
  it; when a detector (`js-shell`/`bot-wall`/`empty`/`error`) shows the static page isn't
  enough, the loop escalates to tier 2 and drives the **persistent `browser`** through the
  Vetstoria wizard (location → new client → species → appointment type → calendar) to read the
  times. The tier ladder is the agent's (`tiers:` in `agent.yaml`); the loop climbs only when a
  detector says so, capped by `max_tier`.
- **Strict, grounded output.** The schema is slots-only (`{slots: [{date, time}]}`,
  `additionalProperties: false`) — a volunteered extra field is rejected, and `grounding`
  checks every reported date/time against what the run actually retrieved. A made-up time
  fails the run.
- **Recorded track → cheap replay.** The first successful run records `track.json` (the ordered
  path the model drove). Re-runs **replay it deterministically with no model calls**,
  re-climbing the tier ladder where the path did; the model returns only at the frontier (the
  final extraction). Proven economics: **~$2.17 to pathfind live → ~$0.03/run on replay** (a
  cheap finisher), ≈75× cheaper — tracked in `cost.jsonl`.

## Build your own like this

The cheap path — discover once, iterate offline at ~$0, harden, ship — is the
[Building an agent guide](../../../docs/agent-construction-sequence.md). You can drive the
whole sequence from your own coding harness (Claude Code / Cursor / Codex) over `zu mcp`.

## Secrets

The bundle's **`.env` is gitignored** and loaded for the run — locally, and (mounted with the
bundle) inside the container when `--sandboxed`. Config names the variable
(`api_key_env: EXA_API_KEY`), never the key. `web_search`'s egress is scoped to `api.exa.ai`;
because the booking-page URL is discovered at run time, the fetch tools declare open (but fully
logged) egress — flip `containment: required` and run `--sandboxed` to enforce the container
boundary.
