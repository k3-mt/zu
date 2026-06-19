# zu-backends

Infrastructure adapters: the **`SandboxBackend`** port (provision and run a
tier's environment) and the **`EventSink`** port (persist and query the event
log). These are the durable/isolation seams the core defers behind a port.

## Registered plugins

### Sandbox backends (`zu.backends`)

| Name | Class | Notes |
|------|-------|-------|
| `local-docker` | `LocalDockerBackend` | Runs a tier's container via the local Docker daemon. Network is disabled by default and enabled only for the render tier; *scoped* egress (allowlist / DNS-pinned) is the deferred egress-policy work. Needs the `[docker]` extra. |

### Event sinks (`zu.sinks`)

| Name | Class | Notes |
|------|-------|-------|
| `sqlite` | `SqliteSink` | The durable canonical store: WAL, `synchronous=FULL`, `busy_timeout`, single writer, keyset streaming, idempotent append. |
| `jsonl` | `JsonlSink` | One JSON object per line — a greppable secondary/trace sink that log shippers tail. |

The payload codec seam (`zu_core.codec`) lets a sink encrypt payloads at rest;
an AES-256-GCM codec ships behind the optional `[encryption]` extra
(`zu_backends.encryption`).

## Extend

Implement the `SandboxBackend` or `EventSink` shape, register under
`zu.backends` / `zu.sinks`, and add a deterministic test (inject a fake Docker
client / use a temp DB path — no real daemon needed offline).

## Tests

`uv run pytest packages/zu-backends` — offline.
