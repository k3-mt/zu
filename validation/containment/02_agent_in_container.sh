#!/usr/bin/env bash
# 02 — The whole agent runs INSIDE the hardened container.
#
# WHAT THIS PROVES: SandboxLauncher stands up the topology (internal default-DROP
# network + egress-proxy sidecar + hardened target), execs the agent inside it,
# and returns the Result. The config declares a capability tool (http_fetch), so a
# bare-host run would be REFUSED by the floor (proven in 01) — its SUCCESS here is
# only possible because the launcher set ZU_SANDBOXED in the box, i.e. the agent
# genuinely ran contained. We also confirm the in-box run emitted its event log.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_docker; require_image; require_python
cleanup_zu_resources

head "02 AGENT-IN-BOX: launch the whole agent in $ZU_IMAGE"

out="$(
  cd "$ROOT"
  "$ZU_PYTHON" - <<'PY'
import asyncio, os
from zu_backends.local_docker import LocalDockerBackend
from zu_cli.sandbox import SandboxLauncher

config = {
    "provider": {"name": "scripted", "script": [{"text": '{"ok": true}', "finish": "stop"}]},
    "plugins": {"tools": ["http_fetch"]},   # a capability tool: bare host would REFUSE
    "containment": "required",
}

async def main():
    launcher = SandboxLauncher(backend=LocalDockerBackend(), image=os.environ["ZU_IMAGE"])
    result, events = await launcher.run({"query": "q"}, config, allowlist=["example.com"])
    types = {e["type"] for e in events}
    print("STATUS=" + result.status.value)
    print("VALUE=" + str(result.value))
    print("EVENTS=" + str(len(events)))
    print("HAS_LIFECYCLE=" + str("harness.task.started" in types and "harness.task.completed" in types))

asyncio.run(main())
PY
)"
say "$out"

assert_contains "$out" "STATUS=success"        "the contained agent returned SUCCESS"
assert_contains "$out" "VALUE={'ok': True}"    "the agent's value came back from inside the box"
assert_contains "$out" "HAS_LIFECYCLE=True"    "the in-box run emitted its full event lifecycle"

say ""; ok "02 AGENT-IN-BOX: PASS"
