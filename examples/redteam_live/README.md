# Red-team container form — live integration

The deterministic gate (`zu test-plugin`) and the scripted container pipeline run
with no Docker. This directory is the **live** layer: the real egress-proxy
sidecar topology that physically enforces the boundary `RED_TEAM_CONTAINER.md`
describes, exercised against a real Docker daemon.

## What it proves

`live_gate.sh` stands up the sidecar topology and asserts the four facts a fake
cannot show:

| Proof | What it shows |
|-------|---------------|
| **CAPTURE** | a target reaching an allowlisted host is logged by the proxy — the out-of-band record the verdict observers read |
| **REFUSAL** | a target reaching an off-allowlist host is refused (403) and logged `allowed=false` |
| **DEFAULT-DROP** | a target that ignores the proxy and dials out directly has **no route off the internal network** — the proxy is the sole egress |
| **MITM EXFIL** | a secret in an HTTPS query string is decrypted by the proxy and logged — the payload `ExfilBreach` judges |

```
zu-rt-internal (--internal, NO external route)        bridge (external)
  ├── target            ── HTTP(S)_PROXY ──▶ proxy ──────┘
  └── proxy sidecar  ◀───────────────────────┘  (only the proxy bridges out)
```

The target is on the internal network **only**, so its sole path off-box is the
proxy sidecar; the proxy has a second leg on `bridge` to reach the real internet.
With MITM on, the proxy mints a per-run CA (written to a shared volume the target
trusts), decrypts the target's HTTPS, and records the URL/body.

## Run it

```bash
# 1. build the image (installs the workspace incl. zu-redteam + the encryption extra)
docker build -t zu-redteam:live .

# 2. run the live enforcement proofs
examples/redteam_live/live_gate.sh zu-redteam:live

# or as an opt-in test (skipped unless the flag is set)
ZU_REDTEAM_LIVE_DOCKER=1 pytest packages/zu-redteam/tests/test_live_docker.py
```

Expected tail:

```
RESULT: PASS — live enforcement proven (capture · refusal · default-drop · MITM exfil)
```

## The pieces

- **`zu-egress-proxy`** (`zu_backends.egress_proxy:main`) — runs `LocalEgressProxy`
  as the sidecar, streaming each connection as JSONL on stdout (read via
  `docker logs`). Env: `ZU_EGRESS_ALLOWLIST`, `ZU_EGRESS_MITM`, `ZU_EGRESS_CA_OUT`.
- **`zu-redteam-run`** (`zu_redteam.runner:main`) — runs a scenario on real Zu
  inside the target container and emits its event log as JSONL (P1).
- The image — the existing `Dockerfile`, which installs every workspace package
  plus `zu-backends[encryption]` (for the MITM CA) and the two entrypoints above.

## The programmatic gate

Beyond this script, `SidecarContainerGate` (`zu_redteam.sidecar`) runs the *same*
sidecar topology programmatically and judges the run with the out-of-band
observers. It is what `_container_gate` uses under `ZU_REDTEAM_CONTAINER=1`, and it
catches a **real deceptive tool** — `DeceptiveRealFetch`, which actually reaches an
off-allowlist host while reporting an allowed one — via the proxy log
(`test_sidecar_gate_catches_a_real_deceptive_tool`). The in-process gate, seeing
only the tool's reported URL, misses it.

```bash
ZU_REDTEAM_LIVE_DOCKER=1 pytest packages/zu-redteam/tests/test_live_docker.py
```

## Host effects

`docker diff` feeds undeclared filesystem writes to `HostEffect` (ships, tested).
The subprocess/syscall source is the shipped seccomp `SCMP_ACT_LOG` profile
(`zu_backends/seccomp/redteam-audit.json`, applied via the launch `seccomp` key)
parsed by `SeccompAuditMonitor`. The profile is proven to apply live; **reading**
the logged records needs a Linux **auditd** host (not Docker Desktop's VM), so off
such a host the monitor yields nothing rather than failing.

## Publishing the image

Two paths (both need write access to the registry — your action, not the gate's):

```bash
# A. Tagged release — the workflow builds + pushes ghcr.io/<owner>/zu-redteam
git tag v0.1.0 && git push origin v0.1.0     # .github/workflows/redteam-image.yml runs

# B. Manual one-off
echo "$GHCR_TOKEN" | docker login ghcr.io -u <user> --password-stdin
docker buildx build --platform linux/amd64,linux/arm64 \
  -t ghcr.io/<owner>/zu-redteam:latest --push .
```

Once published, point the gate at it:
`ZU_REDTEAM_CONTAINER=1 ZU_REDTEAM_CONTAINER_IMAGE=ghcr.io/<owner>/zu-redteam:latest zu test-plugin <pkg>`.

## Fully closed vs. host-dependent

Closed and live-proven here: capture, refusal, default-DROP, MITM exfil, a real
deceptive tool caught, fs-write / persistent-process / mount-escape host effects.

The one genuinely host-dependent piece: reading seccomp `SCMP_ACT_LOG` records to
catch a *transient* exec (one that exits between `docker top` reads) needs a Linux
host with **auditd** — point `SeccompAuditMonitor` at `/var/log/audit/audit.log`
there. Persistent processes (the realistic backdoor threat) are already caught
everywhere by `DockerTopMonitor`.
