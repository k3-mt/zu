# Containment validation suite

Repeatable, self-documenting proofs that a Zu run is **contained** — each script
states exactly what it tests, asserts the outcome, and cleans up after itself.
They turn the ad-hoc Docker validation into a fixed, re-runnable battery.

## Run it

```bash
cd validation/containment
./run_all.sh                 # build the image, then run every proof, print a summary
./run_all.sh --no-build      # reuse an existing image (skip the build)
ZU_IMAGE=zu:ci ./run_all.sh  # validate a different image tag
```

Or run any proof on its own:

```bash
./00_build_image.sh
./01_floor_failclosed.sh     # (no Docker needed)
./02_agent_in_container.sh
./03_egress_allowlist.sh
./04_proxy_enforcement.sh
```

## What each script proves

| Script | Layer | Proves |
|---|---|---|
| `00_build_image.sh` | image | Builds from `../../Dockerfile`; the `zu-run-contained`, `zu-egress-proxy`, `zu` entrypoints are present; the default user is non-root. |
| `01_floor_failclosed.sh` | host (no Docker) | `containment: required` **refuses** a tool with off-box reach on a bare host; `audit` runs it; inside the sandbox (`ZU_SANDBOXED=1`) `required` permits it. |
| `02_agent_in_container.sh` | launcher | The whole agent runs inside the hardened container and returns its Result + event log. The config carries a capability tool a bare host would refuse — its success here means it genuinely ran contained. |
| `03_egress_allowlist.sh` | egress (core) | A contained agent fetching an **allowlisted** host gets HTTP 200; a **disallowed** host is refused by the proxy (403) and recorded as a `harness.defense.blocked` event. |
| `04_proxy_enforcement.sh` | boundary | The raw proxy proofs (delegates to `../redteam/live_gate.sh`): capture, refusal, default-DROP, and MITM HTTPS body capture. |

## Prerequisites

- **Docker** running (all except `01`).
- The host Python that drives the launcher must have the workspace installed
  (`uv sync`). Override with `ZU_PYTHON=/path/to/python` if not using `.venv`.
- Scripts `03`/`04` need outbound internet (the proxy's bridge leg reaches real
  hosts like `example.com`).

## Knobs

| Env | Default | Meaning |
|---|---|---|
| `ZU_IMAGE` | `zu:test` | Image under test. |
| `ZU_PYTHON` | `../../.venv/bin/python` | Host Python that imports `zu_cli` / `zu_backends`. |

## Where the pieces live

- **Image:** built into the local Docker store from [`../../Dockerfile`](../../Dockerfile) — not a file on disk.
- **Launcher (whole agent in container):** [`../../packages/zu-cli/src/zu_cli/sandbox.py`](../../packages/zu-cli/src/zu_cli/sandbox.py)
- **Sandbox backend / egress proxy:** `../../packages/zu-backends/src/zu_backends/{local_docker,egress_proxy}.py`
- **Fail-closed floor:** `../../packages/zu-core/src/zu_core/security.py`
- **Sandbox-aware fetch:** `../../packages/zu-tools/src/zu_tools/fetch.py`

The same proofs also run under pytest (opt-in): `ZU_SANDBOX_LIVE=1` for the
launcher (`packages/zu-cli/tests/test_sandbox.py`) and `ZU_REDTEAM_LIVE_DOCKER=1`
for the proxy gate (`packages/zu-redteam/tests/test_live_docker.py`).
