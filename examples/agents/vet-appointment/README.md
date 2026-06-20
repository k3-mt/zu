# vet-appointment (a bundle)

Find a vet practice's online booking page and return the **next 3 soonest
available appointment slots** — as grounded JSON. A real open-web agent: it
*searches*, *reads*, and is held to its schema **and** to grounding (every slot
and the `booking_url` must actually appear on the page — nothing invented). It
does **not** book anything; it surfaces the slots for a human to book.

```
vet-appointment/
  agent.yaml          # the agent: model, the tier ladder, detectors, the task
  .env                # your secrets (gitignored) — EXA_API_KEY, ANTHROPIC_API_KEY
  .env.example        # copy this to .env
  fixtures/
    booking.html      # a saved page, so the agent is tested offline
```

## Run it

```bash
cp examples/agents/vet-appointment/.env.example examples/agents/vet-appointment/.env
# edit .env: add your EXA_API_KEY (exa.ai) and ANTHROPIC_API_KEY

zu run examples/agents/vet-appointment/              # on your host
zu run examples/agents/vet-appointment/ --sandboxed  # contained on Docker
```

## How it works

- **It searches first.** `web_search` (a built-in tool, Exa connector) turns the
  query into candidate pages (title + url). The agent picks the practice's
  booking page, then `http_fetch`es it — escalating to the tier-2 browser
  (`render_dom`) only if a detector (`js-shell`/`bot-wall`/`empty`/`error`) shows
  the static fetch wasn't enough.
- **The tier ladder is the agent's.** `tiers:` in `agent.yaml` places each tool —
  search + fetch at tier 1, the browser at tier 2 — and the loop climbs only when
  a detector says so (capped by the task's `max_tier`).
- **Nothing is invented.** `grounding` checks every reported value against what
  the run actually retrieved: slot dates/times against the fetched page, the
  `booking_url` against what search returned. A made-up time fails the run.

## Secrets

The bundle's **`.env` is gitignored** and loaded for the run — locally, and
(mounted with the bundle) inside the container when `--sandboxed`. So config names
the variable (`api_key_env: EXA_API_KEY`), never the key. `web_search`'s egress is
scoped to `api.exa.ai`; because the booking-page URL is discovered at run time,
the fetch tools declare open (but fully logged) egress — flip `containment:
required` and run `--sandboxed` to enforce the container boundary.

## Extra pip deps?

If you add a custom tool that needs libraries beyond the base image, drop a
`requirements.txt` here and `zu pack examples/agents/vet-appointment/ -t vet:1`
bakes them into a standalone image.
