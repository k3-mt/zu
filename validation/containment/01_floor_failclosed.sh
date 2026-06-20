#!/usr/bin/env bash
# 01 — The fail-closed containment floor (HOST level, no Docker).
#
# WHAT THIS PROVES: with containment='required', the runtime REFUSES to run a tool
# that has off-box reach (declared egress) on a bare host — it will not silently
# run a capability tool unguarded. The default 'audit' posture runs it (in-process,
# declarations logged). And inside the sandbox (ZU_SANDBOXED=1) 'required' permits
# it, because there the container is the boundary. This is the honesty switch that
# makes "contained" mean something before any Docker is involved.
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_python

head "01 FLOOR: required refuses / audit allows / sandboxed permits"

out="$(
  cd "$ROOT"
  env -u ZU_SANDBOXED "$ZU_PYTHON" - <<'PY'
import asyncio
from zu_core.contracts import TaskSpec
from zu_core.bus import EventBus
from zu_core.loop import run_task
from zu_core.registry import Registry
from zu_core.security import ContainmentRequired
from zu_providers.scripted import ScriptedProvider

class NetTool:                       # declares off-box reach -> needs containment
    name = "net_tool"; tier = 1
    schema = {"name": "net_tool", "parameters": {"type": "object", "properties": {}}}
    prompt_fragment = "net_tool()"
    capabilities = frozenset()
    egress = frozenset({"*"})
    async def __call__(self, ctx): return {"ok": True}

def reg():
    r = Registry(); r.register("tools", "net_tool", NetTool()); return r

def provider():                      # finalises immediately (floor checked at start)
    return ScriptedProvider.from_moves([{"text": '{"ok": true}', "finish": "stop"}])

async def main():
    import os
    # required + bare host -> refused
    try:
        await run_task(TaskSpec(query="q"), provider(), reg(), EventBus(), containment="required")
        print("REQUIRED_BAREHOST=ALLOWED")        # wrong
    except ContainmentRequired as e:
        print("REQUIRED_BAREHOST=REFUSED:" + ",".join(e.tools))

    # audit -> allowed
    r = await run_task(TaskSpec(query="q"), provider(), reg(), EventBus(), containment="audit")
    print("AUDIT_BAREHOST=" + r.status.value)

    # required + sandboxed -> allowed
    os.environ["ZU_SANDBOXED"] = "1"
    r = await run_task(TaskSpec(query="q"), provider(), reg(), EventBus(), containment="required")
    print("REQUIRED_SANDBOXED=" + r.status.value)

asyncio.run(main())
PY
)"
say "$out"

assert_contains "$out" "REQUIRED_BAREHOST=REFUSED:net_tool" "required refuses the capability tool on a bare host"
assert_contains "$out" "AUDIT_BAREHOST=success"            "audit (default) runs it in-process"
assert_contains "$out" "REQUIRED_SANDBOXED=success"        "required permits it inside the sandbox"

say ""; ok "01 FLOOR: PASS"
