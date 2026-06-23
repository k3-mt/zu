# Zu Trusted Computing Base (TCB)

**Audience: anyone layering their own trusted base on top of Zu** — the
Category-1 credential/capability consumer is the proof case, but any serious
consumer needs this. It is the authoritative answer to **ZU-EXT-2**: *exactly*
what is in Zu's trusted core versus a plugin, so a consumer can bound its own
trusted surface and reason about the layered-trust argument.

The governing invariant: **`zu-core` depends only on the standard library and
Pydantic.** It physically cannot import a model SDK, a browser, a cloud SDK, or a
vendor mechanism (WireGuard, SPIFFE/SPIRE, an issuer SDK). Capability lives in
plugins behind typed ports. That invariant is what keeps this TCB small enough to
audit.

---

## 1. In the TCB — `zu-core` (trusted)

Every module here is trusted by every consumer. Each upholds a specific property.

| Module | Trusted property it upholds |
|---|---|
| `contracts.py` | The typed boundaries (`TaskSpec`/`Result`/`Event`). `Event` is a frozen envelope; the namespace validator refuses a mis-typed event at the boundary. Carries the hash-chain fields (`prev_hash`/`hash`). |
| `events.py` | The canonical event taxonomy — one spelling for every `harness.*`/`data.*` type the gate, audit, and projections agree on. |
| `chain.py` | The tamper-evidence hash chain (ZU-AUDIT-1): `event_digest`/`link`/`verify_chain`. Detects reorder, insert, delete, and content edit on replay. stdlib `hashlib`/`json` only. |
| `eventstore.py` | The filter allowlist (and the SQLite injection guard) + the consumer-field registration hook (ZU-AUDIT-3). |
| `sinks.py` | The in-memory canonical store: append-only, idempotent on `event_id`, links each event into its trace chain. |
| `bus.py` | Canonical-store-first ordering: durability before fan-out; links once at the canonical store and fans the linked event to shippers. |
| `registry.py` | Plugin discovery + the kind registry (ZU-EXT-1: `register_kind`) + the interface-major gate. The one registry the loop reads. |
| `ports.py` | The typed Protocols — the trust boundary itself (see §3). Plus the capability-envelope tokens (`CAP_*`/`EGRESS_OPEN`) and the security value-objects (`Severity`, `Verdict`, `Status`). |
| `loop.py` | The interpreter. Holds the model and the tool registry; the model only emits tool-call *signals* it dispatches against the active-tier set (ZU-CORE-1). Runs the pre-execution gate on every call (ZU-CORE-2), mints idempotency keys (ZU-CORE-4), tracks run-level taint (ZU-CD-3), exposes durable grant state (ZU-CD-4), and drives the human-pause/resume state machine (ZU-CD-1/2/5). |
| `grants.py` | The in-memory `GrantStore` — a cache over `harness.grant.updated` events (the log stays the source of truth). |
| `rpc.py` | The out-of-process wire **contract** (ZU-NET-3): the frame codec, `RpcClient`, the `RemoteTool`/`RemoteChannel` forwarding proxies the loop holds, and the generic `serve` loop. No auth/TLS/reconnect — the socket is local and the launcher owns lifecycle. |
| `security.py` | The containment floor (`enforce_containment`) and `SecurityBlock` → `harness.defense.blocked`. |
| `codec.py` | The payload-codec format (plaintext default; AEAD when a cipher is configured). |

The **runtime assumes** (outside its control, also trusted): the operator who
configures the run, the host OS/kernel (process & uid isolation, the loopback
unix socket), and the Python interpreter.

---

## 2. The ports — the trust boundary

A port is a structural `Protocol` in `ports.py`. The core trusts a *shape*, never
a concrete adapter, and assumes a conformant plugin upholds the invariant noted.
Consumers add new kinds with `register_kind` **without editing the core**
(ZU-EXT-1).

| Kind (entry-point group) | Port | Invariant the core assumes |
|---|---|---|
| `providers` (`zu.providers`) | `ModelProvider` | Holds the model credential inside the adapter; returns text + tool-call signals, never a capability handle. |
| `policies` (`zu.policies`) | `Policy` | Decides; emits a typed `Action`, never self-acquires a tool. |
| `tools` (`zu.tools`) | `Tool` | Declares its capability envelope honestly; its side effects match its declaration. |
| `detectors` (`zu.detectors`) | `Detector` | Post-hoc judgement on an observation; pure, side-effect-free. |
| `validators` (`zu.validators`) | `Validator` | Post-hoc judgement on the final result. |
| `gates` (`zu.gates`) | `InvocationGate` | **Pre-execution** allow/deny/escalate on the literal call; deterministic; cannot be disabled (the loop always runs it). **Fails closed (ZU-CORE-2):** a gate that *crashes* judging a capability-bearing or tier-≥2 call becomes a synthesized DENY (rule `gate.crashed.fail_closed`) so a malformed call can't bypass the scope-checker; for an inert tier-1 call it fails open-but-logged (`gate.crashed.skipped`) so a broken gate can't break an ordinary fetch. |
| `backends` (`zu.backends`) | `SandboxBackend` / `OutOfProcessLauncher` | Isolates execution; the OOP launcher gives a plugin its own process/uid. |
| `sinks` (`zu.sinks`) | `EventSink` | Append-only, idempotent; preserves the chain. |
| `triggers` (`zu.triggers`) | `Trigger` | Carries UNTRUSTED inbound payloads; never authoritative. |
| `channels` (`zu.channels`) | `Channel` | Harness-owned external channel; holds its credential inside; exposes typed verbs, never the secret. |
| `workload_identity` (`zu.workload_identity`) | `WorkloadIdentity` | Presents/verifies an attestable identity; the proof carries no private key. A precondition for authz, never the authority. |
| `egress_enforcement` (`zu.egress_enforcement`) | `EgressEnforcement` | Installs default-deny network policy and gates DNS; prevents bypass beneath the policy. |
| `replay_arbiters` (`zu.replay_arbiters`) | `ReplayArbiter` | Decides per replayed rail step (CONTINUE/HANDOFF/ESCALATE/STOP) from the recorded step + live observation (ZU-RAIL-3); the loop honors escalate-to-**human**. The diff metric/thresholds are the consumer's policy. |

---

## 3. NOT in the TCB — plugins (untrusted-until-bounded)

Everything in `zu-providers`, `zu-tools`, `zu-checks`, `zu-backends`,
`zu-huggingface`, and any consumer/sibling package. A plugin's blast radius is
**bounded mechanically**, which is why adding the Nth integration adds capability
but not trusted surface:

- **Capability envelope** (`CAP_*`/`egress`) declares what a tool may do; the
  declaration is on the log and judged out-of-band.
- **Containment floor** refuses an off-box tool unless the run is sandboxed.
- **Sandbox + egress enforcement** bound a tool's host effects and network.
- **Out-of-process boundary** (`rpc.py` + the launcher) puts a secret-bearing
  plugin (the broker) in its own process/uid: a harness compromise yields the
  socket, not the secret (ZU-CORE-3 / ZU-NET-3 / ZU-EXT-4).
- **The gate** vets every invocation before it executes (ZU-CORE-2); a buggy or
  hostile plugin cannot self-acquire an ungranted capability (ZU-CORE-1).

Vendor mechanisms — WireGuard, SPIFFE/SPIRE, a real issuer SDK — are plugins
behind the ports above, **never** imported by the core (ZU-NOT-4).

---

## 4. The audit substrate

- **Append-only + partial-tamper-evident** (`chain.py`): the per-trace hash chain
  makes reorder/insert/delete/content-edit detectable on replay (`verify_chain`),
  and with the AEAD codec configured an at-rest payload edit additionally fails to
  decrypt. **It is NOT, by itself, evidence against a privileged full rewrite:** an
  attacker with write access to the whole store can edit content and re-link the
  entire trace cleanly, after which `verify_chain` passes. Two composable,
  stdlib-only mechanisms close that gap and are what make the stronger claim:
  - **External anchoring** (always available) — the chain head (`chain_head`) is
    periodically written to an append-only *anchor the attacker cannot reach* (a
    separate file, external log, or notary; reference `zu_backends.anchor.JsonlAnchor`).
    `verify_against_anchor` re-derives the head at each anchored seq and fails on a
    mismatch, so a full rewrite is caught. This is the mechanism that detects a
    privileged full rewrite; bare chaining does not.
  - **HMAC signing** (only when a signing key is configured) — `link` adds an
    HMAC-SHA256 over the digest and `verify_chain(key=…)` checks it, so a content
    edit fails verification even after a clean re-link by an attacker *without the
    key*. It does **not** protect against a compromised harness that holds the key.
    Absent a key, the chain is byte-for-byte unchanged.
- **Out-of-band facts the judged cannot author**: `harness.egress.observed` /
  `harness.host_effect.observed` (from the proxy and host monitor), preferred
  over a plugin's self-report.
- **Decision provenance** (ZU-AUDIT-2): `harness.gate.decided` records
  allow/escalate/deny + the rule, parented to the `harness.tool.invoked` it
  decided; `harness.approval.requested`/`harness.approval.resolved` record the
  human binding. Replay reconstructs, per action, which rule allowed it or which
  human approved it.
- **Consumer fields** (ZU-AUDIT-3): `payload["ctx"]` carries `grant_id`,
  `consent_ref`, `capability_id`, `peer`, `idempotency_key`; a registered field
  is queryable (SQLite via a side index; memory/jsonl via `event_matches`).

---

## 5. Identity & boundaries — what stays out of the harness

- **Harness-owned channels** (ZU-NET-2): a channel's credential lives in the
  adapter (or, out-of-process, in the worker), never in the policy's context.
- **Workload identity** (ZU-NET-4/5): an attestable principal on a channel,
  recorded per action; an attestation measurement (when required) refuses a
  tampered harness.
- **The OOP memory boundary** (ZU-NET-3): the broker's secret lives only in the
  worker's address space; the harness holds the socket and a forwarding proxy.

---

## 6. Residual risks (what conformance does NOT buy)

Conformance buys the *substrate*, not *safety*. These belong to the consumer and
no mechanism removes them:

- **Prompt injection is bounded, not solved.** Once the model reads hostile
  content its reasoning is suspect; run-level taint lets you *escalate*
  high-consequence actions, it does not clean the reasoning.
- **Consent comprehension** is a security property and the softest one: phished
  consent produces a cryptographically perfect chain pointing at the attacker.
- **The unserveable quadrant** (broad scope + novel destination + high value +
  low latency) has no safe architecture and must be *refused*, not finessed. Zu
  provides the mechanism to refuse; the decision is the consumer's.
- **Audit-log tampering is only partially evident from the chain alone.** The hash
  chain catches partial tampering (edit/insert/delete/reorder) on replay, but a
  privileged attacker with write access to the *whole* store can re-link a clean
  chain. Detecting that requires the **external anchor** (an append-only head
  record the attacker cannot reach — §4); without an anchor configured, a full
  rewrite is undetectable. **HMAC signing** raises the bar against an attacker
  *without the key* but is worthless against a **compromised harness** that holds
  the key (and against the harness itself — it is the signer). Neither mechanism
  makes the log evidence against the operator/harness; they bound an *external*
  store attacker.
- **DNS as a covert channel**: closed on the contained path — the
  `SandboxLauncher` routes through `EgressEnforcement`, which pins the proxy by IP
  in the target's `/etc/hosts` and points DNS at a non-resolving nameserver, so
  the embedded resolver cannot be used to exfiltrate. Offline-proven (policy +
  plumbing); live behavior validated under `--run-docker`. A misconfigured
  enforcement mechanism re-opens it.

---

## 7. Extension policy

A consumer adds a port kind with `Registry.register_kind(name, group)` (or the
`zu.kinds` entry-point group) and implementations under that group — **no core
edit**. The interface-major gate (`__zu_interface__`) refuses a plugin built
against an incompatible contract. A major bump means a port's Protocol changed
incompatibly; the TCB above is the set whose majors a consumer pins against.

## 8. Conformance map

The requirement → port/field → proof map is maintained in
[`../zu-upstream-conformance.md`](../zu-upstream-conformance.md) §9, and guarded
by `packages/zu-core/tests/test_conformance_matrix.py` (every MUST has a named,
passing offline proof).
