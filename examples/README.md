# Examples

Runnable, copy-paste examples. (The end-to-end *proof suites* that build images and assert
containment live in [`../validation/`](../validation/), not here.)

## The flagship agent — [`agents/`](agents/)

[`agents/vet-appointment`](agents/vet-appointment/) drives a real, multi-step JavaScript
booking widget and returns 3 grounded appointment slots — search → fetch → a persistent
browser through the wizard. It ships a recorded `track.json`, so re-runs replay the path
deterministically (~$2.17 to pathfind → ~$0.03/run on replay).

```bash
zu run agents/vet-appointment/              # live (needs EXA_API_KEY + OPENROUTER_API_KEY)
zu run agents/vet-appointment/ --sandboxed  # contained, behind the egress proxy
```

Scaffold your own with `zu init`, then follow the
**[Building an agent guide](../docs/agent-construction-sequence.md)** — capture once, iterate
offline at ~$0, harden, and ship.

## Integrations — [`integrations/`](integrations/)

Sample configs to drive Zu from a coding agent over MCP (Claude Code, Cursor, Codex) — design,
run, **and construct** agents in natural language. Copy the one for your client; register
`zu mcp` once.
