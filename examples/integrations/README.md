# Use Zu from your coding agent (MCP)

Live in your harness of choice — Claude Code, Cursor, or any MCP client — and let
it design, validate, run, and inspect Zu agents for you in natural language. One
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

The agent will call:

- `zu_scaffold` — write a starter `zu.yaml` + `task.yaml`
- `zu_validate` — check the config, plugins, and schema
- `zu_run` — execute the task and **stream every step back live** (the model's
  train of thought, tool calls, detector verdicts, escalations), returning a
  concise result + a `run_id`
- `zu_traces` — read the full event log for any `run_id` (the always-on store)
- `zu_plugins` — list what's available to wire

…and can read the resources `zu://plugins` and `zu://config/schema` to design a
valid config without you pasting docs.
