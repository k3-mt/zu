"""A tiny, fully-offline tour of what Zu can do *today* (build steps 1–2).

No API keys, no network. It shows two things that already work: plugin
discovery across every port, and the ScriptedProvider (the fake model) playing
a fixed script back in order — the foundation that makes the whole runtime
testable offline before any real model is wired in.

    uv run python examples/scripted_demo.py
"""

from __future__ import annotations

import asyncio

from zu_core.ports import ModelRequest
from zu_core.registry import GROUPS, Registry
from zu_providers.scripted import ScriptedProvider


def show_plugins() -> None:
    reg = Registry()
    reg.discover()
    print("Discovered plugins (every built-in uses the same plugin API you would):")
    for kind in GROUPS:
        print(f"  {kind:11} {', '.join(reg.names(kind)) or '—'}")


async def play_a_script() -> None:
    print("\nThe fake model plays a fixed script back in order:")
    model = ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": "https://example.com/product/123"}},
            {"tool": "html_parse", "args": {"selector": ".price"}},
            {"text": "the price is $9.00", "finish": "stop"},
        ]
    )
    req = ModelRequest(messages=[{"role": "user", "content": "extract the price"}])
    step = 1
    while not model.exhausted:
        resp = await model.complete(req)
        if resp.tool_calls:
            call = resp.tool_calls[0]
            print(f"  step {step}: call {call.name}({call.args})")
        else:
            print(f"  step {step}: finish -> {resp.text!r}")
        step += 1


def main() -> None:
    show_plugins()
    asyncio.run(play_a_script())


if __name__ == "__main__":
    main()
