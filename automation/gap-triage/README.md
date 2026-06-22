# Gap-triage agent

A zu agent that triages `capability-gap` issues — **zu maintaining zu**. When a
maintainer adds the `capability-gap` label to an issue,
[`.github/workflows/gap-triage.yml`](../../.github/workflows/gap-triage.yml) runs this
agent over the issue and posts a structured triage back as a comment.

It lives outside the `packages/*` workspace (like `community/`) because it's automation,
not part of the shipped runtime.

## What it produces

A JSON triage matching `agent.yaml`'s `output_schema`:
`is_capability_gap`, `root_cause`, `proposed_capability` (the smallest **generic**
primitive that would close it — never a hardcode), `investigation_steps`, `confidence`.

## Security — untrusted issue input cannot exploit it

Anyone can open an issue, so the body is attacker-controllable. Defence in depth:

- **Label gate.** The workflow only fires on the `capability-gap` *label*, which requires
  triage/write permission to apply — opening an issue isn't enough to trigger the agent.
- **No egress + containment.** `tiers: { 1: [recall] }` (no `http_fetch`/`browser`/
  `render_dom`) and `containment: required` — there is no tool that can reach the network,
  so a prompt-injected model has nothing to exfiltrate the model key through.
- **Structural rendering.** The workflow injects the issue via
  `python -m zu_cli.gap_triage render`, which sets `task.query` through a YAML parser (the
  issue is a *string value*). Issue content can never overwrite `provider`/`tiers`/
  `containment`. See [`packages/zu-cli/src/zu_cli/gap_triage.py`](../../packages/zu-cli/src/zu_cli/gap_triage.py).
- **Spotlighting.** The issue is wrapped as `<<UNTRUSTED_ISSUE>>` data, never instructions.
- **Output sanitised.** `@mentions` are neutralised and length capped before posting.

## Run it offline ($0, no model, no network)

```bash
zu run automation/gap-triage/ --offline    # replays fixtures/capture.json
```

This proves the wiring + schema validator at $0. The CI run is live (one model call); the
rendered `task.query` carries the real issue and `provider.model` is injected from `ZU_MODEL`.

## Configuration — vendor-neutral and optional

The agent is wired to **any** OpenAI-compatible provider; no vendor is named or assumed.
It runs in CI only if a key is configured — otherwise the workflow is a clean no-op.

| Setting | Where | Required? | Meaning |
|---|---|---|---|
| `ZU_MODEL_API_KEY` | Actions **secret** | to enable (absent ⇒ skip) | the API key — for whatever provider you use |
| `ZU_MODEL_BASE_URL` | Actions **variable** | optional | the endpoint (adapter default if unset) |
| `ZU_MODEL` | Actions **variable** | optional | the model id |

Point these at OpenAI, OpenRouter, a local vLLM/Ollama, an Anthropic-compatible gateway —
your choice. Swapping providers is changing these values, nothing in code.

## Re-capture the fixture

The shipped `fixtures/capture.json` is a hand-authored finish-only bundle (the agent calls
no tools). If you change `output_schema`, update the `moves[0].text` JSON so it still
satisfies the schema, then re-run the offline replay above to confirm `status: success`.
