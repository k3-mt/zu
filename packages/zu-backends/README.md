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
(`zu_backends.encryption`). Every sink links each event into its trace's
tamper-evidence hash chain (`zu_core.chain`, ZU-AUDIT-1).

### Upstream-conformance reference plugins

Dependency-light reference implementations of the conformance ports (spec:
[`zu-upstream-conformance.md`](../../zu-upstream-conformance.md), trusted base:
[`docs/TCB.md`](../../docs/TCB.md)):

| Kind | Name | Class | Notes |
|------|------|-------|-------|
| `zu.channels` | `credential-broker` | `broker:CredentialBroker` | A harness-owned `Channel` (ZU-NET-2): secret read inside the adapter, narrow verbs (`mint`/`introspect`) return a *derived* token, never the secret. |
| `zu.backends` | `oop-launcher` | `oop_launcher:OutOfProcessLauncher` | Runs a plugin (e.g. the broker) in a separate process/uid over the `zu_core.rpc` socket contract (ZU-NET-3 / ZU-CORE-3): a harness compromise yields the socket, not the secret. |
| `zu.workload_identity` | `static` | `identity:StaticIdentity` | Attestable identity (ZU-NET-4/5): HMAC-signed principal + optional attestation measurement; stdlib-only. mTLS/SPIFFE are follow-ons behind the same port. |
| `zu.egress_enforcement` | `docker-internal-net`, `nftables` | `egress_enforce:*` | Pluggable default-deny + DNS gating port (ZU-NET-1). The `SandboxLauncher` routes through it: pin the proxy by IP + gate the embedded resolver so DNS isn't a covert channel. `ScriptedEnforcement` proves swappability offline; `nftables` is the Linux-native mechanism; live behavior is validated under `--run-docker`. |

`broker.py`'s narrow verbs are the worked example for ZU-EXT-3 (many narrow
typed ports, not one wide "send this request" proxy).

## Extend

Implement the `SandboxBackend` or `EventSink` shape, register under
`zu.backends` / `zu.sinks`, and add a deterministic test (inject a fake Docker
client / use a temp DB path — no real daemon needed offline).

## Tests

`uv run pytest packages/zu-backends` — offline.
