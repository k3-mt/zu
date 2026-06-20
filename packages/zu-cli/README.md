# zu-cli

The `zu` command, the HTTP server, and the MCP server — the surfaces you *drive*
Zu through. This package wires the same runtime path the `import zu` facade uses
(config in → typed `Result` out), so the CLI, the server, and embedding are one
runtime, not three.

This package registers **no plugins**; it consumes them.

## Commands

| Command | What it does |
|---------|--------------|
| `zu run agent.yaml` | Run one task; streams a live trace. `--every 5m` for a scheduled worker, `--no-stream` for CI. |
| `zu demo` | Prove a real run end to end (`--offline` for a scripted self-test). |
| `zu init --template web` | Scaffold a starter `agent.yaml` (`minimal` / `web` / `research`). |
| `zu serve -c agent.yaml` | HTTP service: `POST /run`, `POST /run/stream` (SSE), `GET /healthz`. Needs `[serve]`. |
| `zu deploy local\|compose\|fly\|render\|dockerfile` | Turn a config into a running/deployable service. |
| `zu mcp` | An MCP stdio server so coding agents (Claude Code, Cursor, Codex) drive Zu. Needs `[mcp]`. |
| `zu plugins` | List every plugin the runtime discovered. |
| `zu test-plugin <pkg>` | Run a plugin through the test gate (see `zu-redteam`). |

## Modules

`main.py` (the Typer app), `config.py` (config/task loading + assembly + shared
coercion helpers), `server.py` (FastAPI), `mcp_server.py`, `demo.py`,
`deploy.py`, `scaffold.py`, `trace.py` (the live train-of-thought formatter).

## Tests

`uv run pytest packages/zu-cli` — offline.
