"""A tiny, fully-offline tour of what Zu can do *today* (build steps 1–4).

No API keys, no network. It shows the two halves of the runtime that already
work: plugin discovery across every port, and the interpreter loop driving a
fake model + a fixtured page to a validated Result, with the whole run captured
in the event log.

    uv run python examples/scripted_demo.py
"""

from __future__ import annotations

import asyncio

import httpx

from zu_core.bus import EventBus
from zu_core.contracts import TaskSpec
from zu_core.loop import run_task
from zu_core.registry import GROUPS, Registry
from zu_providers.scripted import ScriptedProvider
from zu_tools.fetch import HttpFetch
from zu_tools.parse import HtmlParse

_PAGE = "<html><body><h1>Acme Widget</h1><span class='price'>$9.00</span></body></html>"


def show_plugins() -> None:
    reg = Registry()
    reg.discover()
    print("Discovered plugins (every built-in uses the same plugin API you would):")
    for kind in GROUPS:
        print(f"  {kind:11} {', '.join(reg.names(kind)) or '—'}")


def _fixtured_registry() -> Registry:
    """A registry whose http_fetch returns a saved page — no network."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_PAGE)

    reg = Registry()
    reg.register("tools", "http_fetch", HttpFetch(allow_private=True, transport=httpx.MockTransport(handler)))
    reg.register("tools", "html_parse", HtmlParse())
    return reg


async def run_a_task() -> None:
    print("\nThe loop drives the fake model + a saved page to a validated Result:")
    provider = ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": "https://example.com/product/123"}},
            {"text": '{"title": "Acme Widget", "price": "$9.00"}', "finish": "stop"},
        ]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="extract the title and price"), provider, _fixtured_registry(), bus)

    print(f"  result : {result.status.value} -> {result.value}")
    print("  events :")
    for e in await bus.query():
        print(f"    {e.type:28} (source={e.source})")


def main() -> None:
    show_plugins()
    asyncio.run(run_a_task())


if __name__ == "__main__":
    main()
