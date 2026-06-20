# Validation suites

End-to-end **proof suites** — they build the image, stand up real Docker, and
*assert* the framework's hardest guarantees. These are maintainer/CI tooling, not
learning examples (those live in [`../examples/`](../examples/)).

| Suite | Proves | Run |
|---|---|---|
| [`containment/`](containment/) | the whole agent runs inside a hardened container; the fail-closed floor; egress allow/block; proxy enforcement | `cd containment && ./run_all.sh` |
| [`redteam/`](redteam/) | the live egress-proxy enforcement gate: capture · refusal · default-DROP · MITM exfil | `bash redteam/live_gate.sh zu:test` |

These also run under pytest behind the `docker` marker (opt in with
`--run-docker`): `packages/zu-cli/tests/test_sandbox.py` and
`packages/zu-redteam/tests/test_live_docker.py`. CI runs them in the `docker` job
(see `.github/workflows/ci.yml`).
