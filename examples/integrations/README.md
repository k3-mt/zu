# Use Zu from your coding agent (MCP)

Live in your harness of choice — Claude Code, Cursor, or any MCP client — and let
it design, run, **construct**, and harden Zu agents for you in natural language. One
stdio server (`zu mcp`) works across all of them; you just register it once.

```bash
pip install 'zu-runtime[mcp]'      # adds the `zu mcp` server
```

The harness launches `zu mcp` as a child process per session (stdio — no port,
no daemon, killed when the session ends). Nothing runs until you ask.

## Register it

- **Claude Code** — `claude mcp add --transport stdio zu -- zu mcp`, or copy
  [`claude-code.mcp.json`](claude-code.mcp.json) to your project's `.mcp.json`.
- **Cursor** — copy [`cursor.mcp.json`](cursor.mcp.json) to `.cursor/mcp.json`
  (project) or `~/.cursor/mcp.json` (global).
- **OpenAI Codex CLI** — append [`codex-config.toml`](codex-config.toml) to
  `~/.codex/config.toml` (MCP support is version-dependent — verify yours).

## Then just talk to your agent

> "Use zu to build a web-extraction agent that pulls a product's name and price,
> validate it, then run it and show me what it does."

or, to construct a browser agent cheaply:

> "Pathfind this booking site with `zu_explore`, save the bundle, then `zu_build` and
> `zu_harden` it offline and show me the readiness gate."

The agent drives these tools — one stdio server exposes them all:

**Design & run**
- `zu_scaffold` — write a starter `agent.yaml`
- `zu_validate` — check the config, plugins, and schema
- `zu_run` — execute a task and **stream every step back live** (train of thought, tool
  calls, detector verdicts, escalations), returning a result + a `run_id`
- `zu_traces` — read the full event log for any `run_id`; `zu_plugins` — list what's wireable

**Discover** (your harness model pathfinds a live site; the trail becomes the agent's path)
- `zu_explore` — drive `http_fetch` / `render_dom` / a persistent `browser` one step at a time
- `zu_explore_save` — project the exploration into `fixtures/capture.json` (a replayable bundle)

**Construct** (offline, ~$0 — no model, no network)
- `zu_offline_run` (replay) · `zu_build` (build → track → harden) · `zu_harden` (resilience) ·
  `zu_construct` (the anti-hardcode readiness gate, G1–G3)

**Contribute**
- `zu_report_gap` — if zu genuinely can't do something, build a strong, **reproducible** issue
  for the repo (the fixtures bundle is the repro) instead of hardcoding around it

…and can read the resources `zu://plugins`, `zu://config/schema`, and `zu://contributing`.
