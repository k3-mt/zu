"""The out-of-process plugin worker (ZU-NET-3).

Runs in a SEPARATE process (and, when launched with privilege, a separate uid)
from the harness. It imports the real plugin — the secret-bearing code, e.g. a
credential broker — and serves it over the unix-socket RPC contract from
``zu_core.rpc``. The plugin's secret is read HERE, inside this address space, so
it never enters the harness's memory: a harness compromise yields the socket, not
the credential. This is what makes ZU-CORE-3 / ZU-EXT-4 mechanical rather than a
convention.

Configuration comes entirely from the environment the launcher sets:
  ZU_OOP_SOCK    unix socket path to serve on
  ZU_OOP_TARGET  import ref of the plugin, "module:Attr"
  ZU_OOP_KIND    "tool" | "channel"
  ZU_OOP_ARGS    JSON object of constructor kwargs (optional)
  ZU_OOP_UID     numeric uid to drop to before serving (optional; best-effort,
                 requires the worker to start privileged)
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
from types import SimpleNamespace
from typing import Any

from zu_core.ports import ChannelRequest
from zu_core.rpc import serve


def _load(ref: str) -> Any:
    module_name, _, attr = ref.partition(":")
    if not attr:
        raise ValueError(f"target ref must be 'module:Attr', got {ref!r}")
    mod = importlib.import_module(module_name)
    return getattr(mod, attr)


def _maybe_drop_privilege() -> None:
    uid = os.environ.get("ZU_OOP_UID")
    if uid and hasattr(os, "setuid") and os.getuid() == 0:
        os.setuid(int(uid))  # best-effort hardening; the process boundary stands regardless


def _tool_spec(tool: Any) -> dict:
    return {
        "name": getattr(tool, "name", "tool"),
        "schema": getattr(tool, "schema", {}),
        "tier": int(getattr(tool, "tier", 1)),
        "prompt_fragment": getattr(tool, "prompt_fragment", ""),
        "capabilities": sorted(getattr(tool, "capabilities", ()) or ()),
        "egress": sorted(getattr(tool, "egress", ()) or ()),
    }


def _build_handler(kind: str, plugin: Any):
    if kind == "channel":
        async def _channel_handler(method: str, args: dict) -> dict:
            if method != "call":
                raise ValueError(f"unknown channel method: {method!r}")
            resp = await plugin.call(ChannelRequest(op=args["op"], args=args.get("args", {})))
            return resp.model_dump()
        return _channel_handler

    async def _tool_handler(method: str, args: dict) -> dict:
        if method == "spec":
            return _tool_spec(plugin)
        if method == "invoke":
            # The harness's RunContext is NOT serialised across the boundary; the
            # worker gets a minimal stub so an OOP tool that reads ctx fields does
            # not crash (it cannot see the harness's run state by design).
            ctx = SimpleNamespace(idempotency_key=None, grants=None, tainted=False, events=[])
            result = plugin(ctx, **args.get("args", {}))
            if asyncio.iscoroutine(result):
                result = await result
            return result
        raise ValueError(f"unknown tool method: {method!r}")

    return _tool_handler


def main() -> None:
    sock = os.environ["ZU_OOP_SOCK"]
    ref = os.environ["ZU_OOP_TARGET"]
    kind = os.environ.get("ZU_OOP_KIND", "channel")
    ctor_args = json.loads(os.environ.get("ZU_OOP_ARGS", "{}"))

    _maybe_drop_privilege()
    target = _load(ref)
    plugin = target(**ctor_args) if isinstance(target, type) else target
    handler = _build_handler(kind, plugin)
    asyncio.run(serve(sock, handler))


if __name__ == "__main__":  # pragma: no cover - exercised via the launcher subprocess
    main()
