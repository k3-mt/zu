#!/usr/bin/env bash
# 03 — Egress allowlist enforcement on a CONTAINED agent (the core property).
#
# WHAT THIS PROVES: inside the box, a tool's network egress goes through the proxy,
# which enforces the allowlist on the internal default-DROP network:
#   * an ALLOWLISTED host (example.com) is fetched successfully (HTTP 200);
#   * a DISALLOWED host (example.org) is REFUSED by the proxy (403) and recorded as
#     a harness.defense.blocked event — visible, not a silent failure.
# Same agent, same image, same launcher; only the allowlist differs. Needs outbound
# internet (the proxy's bridge leg reaches the real hosts).
source "$(dirname "${BASH_SOURCE[0]}")/lib.sh"
require_docker; require_image; require_python
cleanup_zu_resources

run_case() {  # run_case URL ALLOWLIST_JSON
  cd "$ROOT"
  ZU_URL="$1" ZU_ALLOW="$2" "$ZU_PYTHON" - <<'PY'
import asyncio, json, os
from zu_backends.local_docker import LocalDockerBackend
from zu_cli.sandbox import SandboxLauncher

url = os.environ["ZU_URL"]
allow = json.loads(os.environ["ZU_ALLOW"])
config = {
    "provider": {"name": "scripted", "script": [
        {"tool": "http_fetch", "args": {"url": url}},
        {"text": '{"done": true}', "finish": "stop"},
    ]},
    "plugins": {"tools": ["http_fetch"]},
    "containment": "required",
}

async def main():
    launcher = SandboxLauncher(backend=LocalDockerBackend(), image=os.environ["ZU_IMAGE"])
    _result, events = await launcher.run({"query": "fetch"}, config, allowlist=allow)
    fetched = [e for e in events if e["type"] == "data.source.fetched"]
    blocked = [e for e in events if e["type"] == "harness.defense.blocked"]
    ret = [e for e in events if e["type"] == "harness.tool.returned"]
    obs = ret[0]["payload"]["observation"] if ret else {}
    print("FETCHED=" + str(len(fetched)))
    print("BLOCKED=" + str(len(blocked)))
    print("STATUSCODE=" + str(obs.get("status", "")))
    print("BLOCKKIND=" + str(obs.get("blocked", "")))

asyncio.run(main())
PY
}

head "03a ALLOW: example.com WITH example.com on the allowlist"
allow_out="$(run_case "https://example.com/" '["example.com"]')"
say "$allow_out"
assert_contains "$allow_out" "FETCHED=1"      "allowlisted host was fetched through the proxy"
assert_contains "$allow_out" "STATUSCODE=200" "allowlisted fetch returned HTTP 200"
assert_contains "$allow_out" "BLOCKED=0"      "no defense block on the allowlisted host"

head "03b BLOCK: example.org WITHOUT it on the allowlist"
block_out="$(run_case "https://example.org/" '["example.com"]')"
say "$block_out"
assert_contains "$block_out" "FETCHED=0"               "disallowed host was NOT fetched"
assert_contains "$block_out" "BLOCKED=1"               "the refusal was recorded as a defense"
assert_contains "$block_out" "BLOCKKIND=fetch_blocked" "blocked observation is marked fetch_blocked"

say ""; ok "03 EGRESS-ALLOWLIST: PASS"
