# zu-cli

The `zu` command, the HTTP server, and the MCP server — the surfaces you *drive*
Zu through. This package wires the same runtime path the `import zu` facade uses
(config in → typed `Result` out), so the CLI, the server, and embedding are one
runtime, not three.

This package registers **no plugins**; it consumes them.

## Commands

| Command | What it does |
|---------|--------------|
| `zu run agent.yaml` | Run one task; streams a live trace. `--every 5m` for a scheduled worker, `--no-stream` for CI, `--sandboxed` to run contained. |
| `zu init --template web` | Scaffold a starter `agent.yaml` (`minimal` / `web` / `research`). |
| `zu demo` | Prove a real run end to end (`--offline` for a scripted self-test). |
| `zu serve -c agent.yaml` | HTTP service: `POST /run`, `POST /run/stream` (SSE), `GET /healthz`. Needs `[serve]`. |
| `zu deploy local\|compose\|fly\|render\|dockerfile` · `zu pack` | Turn a config/bundle into a running/deployable service or image. |
| `zu mcp` | An MCP stdio server so coding agents (Claude Code, Cursor, Codex) drive Zu — design/run, **construct**, explore, and report capability gaps. Needs `[mcp]`. |
| `zu plugins` · `zu test-plugin <pkg>` | List discovered plugins · run a plugin through the test gate (see `zu-redteam`). |

**The construction sequence** — `task + site → production agent`, frontier spend bounded to one live capture (see [`docs/agent-construction-sequence.md`](../../docs/agent-construction-sequence.md)):

| Command | What it does |
|---------|--------------|
| `zu capture <agent>` | Drive the target **once** (live) → `fixtures/capture.json`. The one live spend. |
| `zu run <agent> --offline` | Replay the captured bundle at **~$0** (no model/network) — the free construction inner loop. |
| `zu build <agent>` | The offline spine: build → record track → harden, gated on resilience. |
| `zu harden <agent>` | Score a captured path against perturbed fixtures (offline brittleness audit + resilience). |
| `zu construct <agent> [--check\|--sandboxed]` | The anti-hardcode readiness gate (G1–G3) / the autonomous, contained construction loop. |

## Modules

`main.py` (the Typer app), `config.py` (config/task loading + assembly + shared coercion
helpers), `server.py` (FastAPI), `mcp_server.py`, `demo.py`, `deploy.py`, `scaffold.py`,
`trace.py` (the live train-of-thought formatter). The construction surface:
`offline.py` (replay + `FixtureSessionBackend`), `build.py`, `harden.py`, `guardrails.py`,
`construct.py` (the meta-agent driver + `LiveStrategist`), `construct_sandbox.py` (contained
construction), `explore.py` (harness-driven pathfinding), `contribute.py` (capability-gap
issues).

## Tests

`uv run pytest packages/zu-cli` — offline. Fixture agents the suite drives live in
[`tests/agents/`](tests/agents/) (the sole shipped example is `examples/agents/vet-appointment/`).
