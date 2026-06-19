# Zu Red Team — Out-of-Band Container Enforcement (Design)

> This is the design for the deferred keystone in `RED_TEAM.md` §1: turning the
> red-team gate from **observe-after-the-fact and cooperative** into **enforce
> from outside the box**. It closes the one gap the in-process gate cannot:
> a *deliberately deceptive* tool that reaches one host and reports another.
> Status: **design / not yet built.** The phased plan and the tracking checklist
> at the end are the unit of work.

---

## 1. The gap, precisely

The shipping deterministic gate is honest about its boundary (`RED_TEAM.md` §1,
`verdict._reached_urls`): every egress fact the judge reads is **self-reported by
the tool**. The chain is:

```
tool.__call__()  ->  returns {"url": "...", "html": "..."}   (the tool's own claim)
loop._invoke     ->  emits data.source.fetched with that obs (loop trusts the obs)
EgressBreach     ->  reads source.fetched["url"]             (judge trusts the log)
```

Every link trusts the tool's word. Against an *honest-but-buggy* plugin this has
real teeth — `LeakyFetch` declares no egress yet reports reaching
`exfil.attacker.example`, and `EgressBreach` fires. But a *deceptive* tool just
lies in the observation: connect to `evil.com`, report `allowed.com` (or omit the
URL, or exfiltrate over a non-HTTP side channel the log never sees). Nothing
in-process can catch that, because the thing doing the reporting is the thing
under suspicion.

The fix is the principle the whole runtime already rests on, applied to egress:
**the judge must read a fact the judged cannot author.** That means watching the
network from *outside* the tool — a boundary the plugin cannot reach or forge.

This design also covers the two adjacent gaps from `RED_TEAM.md` §1:

- **#2 — the container gate is a smoke test.** `gate._container_gate` today only
  proves the hardened container *stands up*; it never runs the target or the
  corpus inside it. We make it run the real scenario in the box.
- **#3 — `HostEffect` is declaration-only.** It fires on a *declared*
  `fs:write`/`subprocess` capability, never on an *observed, undeclared* host
  effect at runtime. We add real syscall/filesystem observation.

---

## 2. Goals and non-goals

**Goals**

1. The reached-host set the judge reads is produced **outside** the target
   container, by an egress proxy that is the container's *only* path off-box.
2. A tool that ignores the proxy and dials a socket directly **cannot connect** —
   the network policy default-drops everything except the proxy hop.
3. The existing observers (`EgressBreach`, `ExfilBreach`, `HostEffect`) keep their
   verdict role and their out-of-band boundary; only their **inputs** improve
   (authoritative egress/host-effect facts instead of self-reports).
4. The verdict stays **deterministic and reproducible**, and the whole
   collect→merge→judge pipeline runs in **CI without a live Docker daemon**, via a
   scripted backend that replays saved proxy + audit logs — exactly the pattern
   the loop already uses for the scripted `SandboxBackend`.
5. There is a **proof both ways**: a fixture that is caught *only* by the container
   form (reaches `evil.com`, reports `allowed.com`) and is missed in-process — the
   empirical demonstration that the new enforcement is real.

**Non-goals**

- Replacing the in-process gate. It remains the always-on floor; the container
  form is the escalation on top, behind `ZU_REDTEAM_CONTAINER=1`.
- A general multi-tenant sandbox product. This is the red-team gate's enforcement
  substrate, reusing the existing `SandboxBackend` port.
- Kernel-exploit-grade escape resistance (microVM/gVisor). That is a later
  `SandboxBackend` adapter; the port is designed so it drops in without loop or
  verdict changes.

---

## 3. Architecture

The three trust boundaries from `RED_TEAM.md` §1 become *physical*: a host-side
control plane, an isolated target container, and an egress proxy that sits
between the container and the network and reports to the control plane.

```
        ┌──────────────────────── CONTROL PLANE (host, out of band) ───────────────────────┐
        │  zu_redteam.container.ContainerGate                                               │
        │    • launches the target container + the proxy on an isolated network             │
        │    • after the run, COLLECTS three logs and merges them into one ObservedRun:     │
        │        (a) in-container event log   (b) proxy connection log   (c) audit log      │
        │    • runs the SAME verdict observers host-side  ──►  PASS / FAIL                   │
        └───────────▲──────────────────────────▲───────────────────────────▲───────────────┘
                    │ (a) events.jsonl          │ (b) connections.jsonl     │ (c) audit.jsonl
                    │ (stdout of the runner)    │ (proxy, authoritative)    │ (seccomp/fanotify)
        ┌───────────┴───────────────┐   ┌───────┴──────────────┐   ┌────────┴───────────────┐
        │ TARGET CONTAINER          │   │ EGRESS PROXY (sidecar)│   │ HOST-EFFECT MONITOR     │
        │  zu-redteam-run entrypoint│   │  sole route off-box;  │   │  observes syscalls / fs │
        │  • registers target+      │──►│  logs every CONNECT/  │   │  writes from outside    │
        │    neighbours, real Zu    │   │  request; ENFORCES the│   │  the target's userland  │
        │  • runs the corpus scenario│  │  declared allowlist   │   └─────────────────────────┘
        │  • planted secret inside  │   └──────────────────────┘
        │  • NO default route: the  │     the proxy is the only gateway; a direct socket
        │    proxy is the only egress│    to evil.com has nowhere to go (default-DROP)
        └───────────────────────────┘
```

### 3.1 The egress proxy is the ground truth

A forward proxy (CONNECT-capable) runs as a sidecar. The target container is
attached to a Docker **internal** network with no gateway, plus a link to the
proxy; iptables/nft default-policy is DROP for everything except the proxy hop.
The container's environment carries `HTTP_PROXY`/`HTTPS_PROXY` so well-behaved
clients route through it — but the *enforcement* is the network policy, not the
env var: a tool that opens a raw socket to an arbitrary IP simply cannot route.

The proxy does two jobs:

- **Logs** every connection as a JSONL line: `{ts, client, host, ip, port,
  scheme, bytes_out, allowed}`. This log — not the tool's observation — is what
  `EgressBreach`/`ExfilBreach` read. The tool can no longer lie about where it
  went, because it is not the one reporting.
- **Enforces** the declared allowlist at the proxy: a CONNECT to a host outside
  the target's declared `egress` (and not `EGRESS_OPEN`) is **refused** and logged
  with `allowed: false`. This is the "attempt hits a wall" the §1 vision
  describes; the breach is the *attempt*, recorded out of band.

TLS is handled in two tiers (see phasing): the CONNECT host is always visible
(enough for the allowlist), and an opt-in MITM CA lets the proxy see request
bodies/query strings (needed for `ExfilBreach` to spot the planted secret encoded
into an HTTPS URL).

### 3.2 The in-container runner

A small entrypoint, `zu-redteam-run`, ships in the image. It reads a scenario
spec on stdin/argv (which plugins to register, the scripted moves, the planted
secret), runs `zu_core.loop.run_task` **in-process inside the container**, and
writes the canonical event log to stdout as JSONL. This is the same `run_scenario`
the in-process gate uses (`zu_redteam.harness`), just executed in the box. The
control plane reads stdout for log (a).

The image is the existing render image plus the zu packages and this entrypoint
(the `Dockerfile` already installs all workspace packages; we add `zu-redteam`
and the console-script). It must be **published** (e.g.
`ghcr.io/k3-mt/zu-redteam:<tag>`) so CI/users pull a known-good image rather than
build per-run.

### 3.3 Facts flow back as out-of-band events

The control plane merges the three logs into the existing `ObservedRun`. Egress
and host-effect facts enter the canonical log as **new event types emitted by the
harness** (`source = "harness"`), never by the tool:

- `harness.egress.observed` — `{client, host, ip, port, scheme, bytes_out,
  allowed}`, one per proxy connection.
- `harness.host_effect.observed` — `{kind: "fs:write"|"subprocess"|..., path|argv,
  pid}`, one per observed host effect from the audit log.

These are appended to the in-container event log after collection, producing a
single `ObservedRun` the observers judge exactly as today. Because they are
harness-attributed and out of band, the attacker still cannot reach or forge them
(the §1 invariant holds).

> **Taxonomy note (required):** new event types must be added to
> `zu_core.events` and to `HARNESS_TYPES`/`ALL_TYPES`, or `ProvenanceBreach` will
> (correctly) flag them as outside the known taxonomy. This is a deliberate, small
> core change, gated by the same provenance rules.

---

## 4. Component changes

| Component | Change |
|-----------|--------|
| `zu_core.events` | Add `EGRESS_OBSERVED`, `HOST_EFFECT_OBSERVED`; extend `HARNESS_TYPES`/`ALL_TYPES`. |
| `zu_core.ports` | Add an `EgressProxy` Protocol: `launch(spec) -> handle`, `connections(handle) -> list[dict]`, `close(handle)`. Keep `SandboxBackend` as-is; extend its `launch` **spec** (already a free-form dict) with `network: "isolated"`, `proxy`, `ca_cert`, `seccomp`, `audit`. No port-shape break. |
| `zu_backends.local_docker` | Implement scoped egress: attach the container to an internal network + proxy link, default-DROP, inject proxy env. This is the "deferred egress-policy work" the current `launch` docstring names. Add a generic `exec_entrypoint(sandbox, argv, stdin)` so the runner (not just `zu-render`) can be exec'd. |
| `zu_backends` (new) | `egress_proxy.py` — a `LocalEgressProxy` (CONNECT proxy, JSONL connection log, allowlist enforcement, optional MITM CA). Optional dep, lazily imported like the docker SDK. |
| `zu_backends` (new) | `ScriptedSandbox` + `ScriptedEgressProxy` — replay a saved event log / connection log, so the full pipeline runs in CI with no daemon (mirrors `ScriptedProvider`). |
| `zu_redteam.container` (new) | `ContainerGate`: orchestrates launch → run corpus in box → collect (a)(b)(c) → merge into `ObservedRun` → run observers. The real meat of #1/#2. |
| `zu_redteam.verdict` | `EgressBreach`/`ExfilBreach`: prefer `EGRESS_OBSERVED` facts when present (authoritative); fall back to `_reached_urls` only when running the in-process form. `HostEffect`: also fire on observed `HOST_EFFECT_OBSERVED` (not just declared caps). Boundary and determinism unchanged. |
| `zu_redteam.gate._container_gate` | Replace the smoke test with a real `ContainerGate` run of the corpus; keep the honest SKIP path when Docker/image absent. |
| `zu_redteam.fixtures` | Add `DeceptiveFetch` (reaches `evil.example`, *reports* `allowed.example`) and its saved proxy log — the fixture that is caught only by the container form. |
| image / `Dockerfile` | Add `zu-redteam` + the `zu-redteam-run` console script; publish `ghcr.io/k3-mt/zu-redteam`. |

---

## 5. Phasing

Each phase is independently shippable and ends with a concrete acceptance test.
The CI-testable pipeline (scripted backend) lands first so every later phase has
a deterministic harness.

**P0 — Scaffolding & the scripted pipeline (no Docker).**
- New event types + taxonomy entries.
- `ScriptedSandbox`/`ScriptedEgressProxy` replaying saved logs.
- `ContainerGate` collect→merge→judge wired against the scripted backend.
- `EgressBreach`/`ExfilBreach`/`HostEffect` extended to read the new facts.
- **Accept:** a unit test where `ScriptedEgressProxy` reports a connection to
  `evil.example` while the in-container event log's `source.fetched` says
  `allowed.example` → `EgressBreach` FIRES on the proxy fact and the deception is
  caught; the same `ObservedRun` judged by the *in-process* path (self-report
  only) PASSes. The diff between the two is the proof of #1.

**P1 — Real proxy + locked network (host allowlist, reached-host truth).**
- `LocalEgressProxy` (CONNECT proxy + JSONL log + allowlist refusal).
- `local_docker` isolated-network + default-DROP + proxy env; `exec_entrypoint`.
- `zu-redteam-run` entrypoint + published image.
- `_container_gate` runs the corpus in the box behind the proxy.
- **Accept (opt-in, live Docker):** the corpus PASSes a benign tool in-container;
  `DeceptiveFetch` FAILs via the proxy log; a tool that opens a raw socket to a
  non-allowlisted IP gets connection-refused (default-DROP), recorded as
  `allowed: false`.

**P2 — TLS MITM for payload/exfil inspection.**
- Proxy MITM CA, trusted only inside the container; proxy logs HTTPS request
  lines/bodies.
- `ExfilBreach` reads proxy request payloads for the planted secret (DNS/query
  exfil over HTTPS).
- **Accept:** a tool that base64s the secret into an HTTPS query string is caught
  by `ExfilBreach` from the proxy log, not the (clean) self-report.

**P3 — Host-effect observation (real #3).**
- A host-effect monitor (seccomp-bpf syscall log and/or `fanotify`/audit for fs
  writes & process spawns) writing the audit log from outside the target's
  userland.
- `HostEffect` fires on *observed, undeclared* fs-write/subprocess, not only on a
  declared capability.
- **Accept:** a tool that writes outside its scope or spawns a process — while
  declaring neither — is caught by `HostEffect` from the audit log.

---

## 6. Testing strategy

Same split the repo already uses for models and the browser sandbox:

- **Deterministic, always-on (CI):** the scripted backend (P0) replays saved
  proxy/audit/event logs, so `ContainerGate` and the extended observers are fully
  exercised without a daemon. Every fixture (benign, leaky, deceptive, exfil,
  host-effect) has a frozen log. This is where the "caught only in-container"
  proof lives.
- **Opt-in, live (`ZU_REDTEAM_CONTAINER=1` + Docker):** the real proxy + real
  container + published image, exercised against a real daemon the way live model
  providers are. Absent Docker/image it SKIPs honestly (unchanged policy); an
  infra error SKIPs, never FAILs a plugin.

A discovered real breach freezes into a corpus case (its logs become a scripted
fixture), so the container findings feed the same growing regression suite.

---

## 7. Security of the gate itself

- **The MITM CA is a gate secret.** It is generated per run, trusted *only* inside
  the ephemeral target container, and destroyed with it. It never touches the host
  trust store. (Document loudly; it is the one new sharp edge.)
- **The proxy is in the control plane, not reachable by the target** except as a
  forward proxy hop — the target cannot read the proxy's log or config. The
  verdict boundary (`RED_TEAM.md` §1 rule 2) is preserved physically.
- **Default-DROP is the enforcement; the proxy env is a convenience.** A breach is
  defined by what the proxy/audit logs show, never by whether the tool cooperated.
- **Resource bounds** (`ResourceBreach`) extend naturally: the container carries
  the existing cap-drop/no-new-privileges/pids-limit hardening; cgroup CPU/mem
  caps and a wall-clock teardown bound the run.

---

## 8. Open decisions

1. **Proxy implementation:** a small custom asyncio CONNECT proxy (no dep, full
   control of the log format, MITM is extra work) vs. `mitmproxy` (MITM for free,
   heavier dep, must script its addon). *Lean:* custom for P1, evaluate mitmproxy
   for P2.
2. **Host-effect mechanism (P3):** seccomp-bpf audit log vs. `fanotify` vs. an
   eBPF agent. *Lean:* start with a seccomp profile that logs (not kills) the
   sensitive syscalls; eBPF later if needed.
3. **One image or two:** fold the runner into the existing render image, or ship a
   separate `zu-redteam` image. *Lean:* one image, extra entrypoint — fewer things
   to publish and pin.
4. **Allowlist source of truth:** the proxy enforces the union of the target
   tools' declared `egress`; confirm that matches what the observers expect when a
   tool declares `EGRESS_OPEN` (open-egress → proxy allows, containment judged by
   `ExfilBreach`/host-effect instead, exactly as §6.1 already specifies).

---

## 9. Tracking checklist

P0 — scripted pipeline (no Docker) ✅ **landed**
- [x] `events`: add `EGRESS_OBSERVED`, `HOST_EFFECT_OBSERVED` + taxonomy
- [x] `ports`: add `EgressProxy` Protocol; document extended `SandboxBackend` spec
- [x] `zu_backends.scripted_sandbox`: `ScriptedSandbox`, `ScriptedEgressProxy`
- [x] `zu_redteam.container.ContainerGate`: collect → merge → judge (`merge_evidence`)
- [x] `verdict`: `EgressBreach`/`ExfilBreach` prefer observed facts; `HostEffect` on observed effects
- [x] fixtures: `DeceptiveFetch` + `deceptive_connections`/`exfil_connections` logs
- [x] **test:** deception caught in-container, missed in-process (`test_container.py`, the #1 proof)

P1 — real proxy + locked network ✅ **landed, live-proven**
- [x] `zu_backends.egress_proxy.LocalEgressProxy` (CONNECT + HTTP forward, JSONL log, allowlist refusal) — unit-tested over loopback
- [x] `local_docker`: `network: "isolated"` + proxy-env injection; `exec_entrypoint` — fake-client tested
- [x] `zu-redteam-run` entrypoint (`zu_redteam.runner`) + `zu-egress-proxy` sidecar entrypoint; `Dockerfile` installs `zu-redteam` + `zu-backends[encryption]`
- [x] `DockerContainerRunner` (launch → exec → parse → collect → merge → judge) — backend/proxy-agnostic, full flow proven in CI with fakes
- [x] `gate._container_gate`: runs a scenario in-container behind the proxy; honest SKIP without Docker/flag
- [x] **LIVE (real Docker):** image built; in-container `zu-redteam-run` verified; sidecar topology proves CAPTURE, REFUSAL, and **DEFAULT-DROP** (`examples/redteam_live/live_gate.sh`, codified as `test_live_docker.py`)
- [x] **`SidecarContainerGate`** — the programmatic gate on the real sidecar topology (`zu_redteam.sidecar`); `_container_gate` now uses it. A **real deceptive tool** (`DeceptiveRealFetch`, which actually reaches an off-allowlist host while lying) is **caught live** by the proxy log (`test_sidecar_gate_catches_a_real_deceptive_tool`)
- [x] publish workflow: `.github/workflows/redteam-image.yml` (build + push to ghcr on tag; optional self-hosted live-gate job)

P2 — TLS MITM for exfil 🟡 **mechanism landed** (live CA-trust pending)
- [x] `MitmCA` — per-run CA, mints leaf contexts on demand, in-container trust only
- [x] `LocalEgressProxy` MITM mode — terminates client TLS with a minted leaf, records the request URL + bounded body, re-originates TLS upstream
- [x] `ExfilBreach` reads the proxy's recorded payload for the planted secret (observer landed in P0)
- [x] CA delivery: `local_docker` bind-mounts the per-run CA read-only + sets `SSL_CERT_FILE`/`REQUESTS_CA_BUNDLE`, cleaned up at destroy; `DockerContainerRunner` ships the CA when the proxy is MITM-enabled
- [x] **test:** HTTPS query-string *and* POST-body secret caught from the proxy log — real TLS client + upstream over loopback (`test_mitm.py`)
- [x] **LIVE (real Docker):** the in-container client trusts the mounted per-run CA, makes a real HTTPS request, and the sidecar proxy decrypts it and logs the secret (`live_gate.sh` proof 4) — MITM proven end to end against a real external host

> **P2 scope, honestly.** The interception is real and proven end-to-end over
> loopback: a client that trusts the per-run CA sends an HTTPS request through the
> proxy, the proxy decrypts it, and the planted secret in the query string (or POST
> body) lands in the connection log that `ExfilBreach` judges. The CA-delivery
> plumbing (bind-mount + trust env + cleanup) is unit-tested with the fake client.
> What only live Docker shows is the in-container client honouring the mounted CA;
> that, plus a gate opt-in flag, is the remaining wiring.

P3 — host-effect observation ✅ **landed** (fs-write · subprocess · mount-escape)
- [x] `HostEffectMonitor` port + `DockerFsDiffMonitor` (reads `docker diff`, flags writes outside the writable scope) + `ScriptedHostMonitor` replay
- [x] `LocalDockerBackend.fs_diff` / `top` / `mounts` — the out-of-band audit sources, read before teardown
- [x] `HostEffect` fires on any observed undeclared host effect (observer landed in P0; now fed by the monitors below)
- [x] `SidecarContainerGate` collects host effects (live sandbox) and folds them into the verdict; `_container_gate` wires a `CompositeHostMonitor` of all three portable sources
- [x] **fs-write:** undeclared write caught through the full flow (`test_container.py`); scope filtering unit-tested
- [x] **subprocess (portable):** `DockerTopMonitor` reads the target's process table and flags an undeclared **persistent** process — no auditd needed, works on Docker Desktop. **Live-proven:** a `BackdoorTool` that spawns `sleep 31337` is caught (`test_sidecar_gate_catches_a_persistent_undeclared_process`).
- [x] **subprocess (transient):** the shipped seccomp `SCMP_ACT_LOG` profile (`redteam-audit.json`, applied via the launch `seccomp` key) + `SeccompAuditMonitor` parsing audit `SECCOMP` records (parser unit-tested). Profile **applies live**; seccomp shown to take effect on the daemon.
- [x] **host-mount escape:** `MountEscapeMonitor` inspects the target's mounts and flags a writable host bind-mount (the one escape `docker diff` can't see); unit-tested.
- [ ] **live audit-read (transient exec only):** reading the `SCMP_ACT_LOG` records needs a Linux **auditd** host (not Docker Desktop's VM) — `SeccompAuditMonitor` reads the audit log when present and yields nothing when absent, never failing. The portable `DockerTopMonitor` covers persistent processes everywhere; auditd is only needed to also catch a process that exits between reads.

> **P3 scope, honestly.** Three host-effect sources ship and feed one observer
> through one event shape: filesystem writes (`docker diff`), processes (`docker
> top`, portable + live-proven), and host-mount escapes (mount inspect). The seccomp
> profile adds transient-exec capture and is proven to *apply*; the only genuinely
> host-dependent piece is *reading* the kernel audit records (auditd), and even
> that only matters for a process too short-lived for `docker top` — so on a daemon
> without auditd the gate still catches the realistic threat (a lingering
> backdoor), degrading to silence on the narrow transient case rather than failing.

Docs
- [ ] update `RED_TEAM.md` §1 status box as each phase lands (observe-after-the-fact → enforced)
</content>
</invoke>
