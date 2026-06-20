"""Pytest fixtures built on the doubles + factories.

These are auto-available wherever zu-testing's plugin is loaded (the whole
workspace, and any downstream package that installs zu-testing to test its own
plugins). All are function-scoped and build fresh state per test, so tests stay
self-contained — no shared mutable globals, no ordering coupling.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from .doubles import FakeSandboxBackend, FakeSink
from .factories import fetch_tool, registry_with, scripted_provider


@pytest.fixture
def fake_sink() -> FakeSink:
    """A fresh in-memory EventSink with a synchronous ``appended`` view."""
    return FakeSink()


@pytest.fixture
def make_sandbox_backend() -> Callable[..., FakeSandboxBackend]:
    """Factory for a daemon-free SandboxBackend (render and/or entrypoint modes)."""
    return FakeSandboxBackend


@pytest.fixture
def make_fetch_tool() -> Callable[..., Any]:
    """Factory for the real http_fetch tool over a mock transport (no network)."""
    return fetch_tool


@pytest.fixture
def agent_runner() -> Callable[..., Awaitable[tuple[Any, list[Any]]]]:
    """Run a scripted agent through the REAL interpreter loop and return
    ``(result, events)``. The one fixture that lets any package — or a third-party
    plugin author — exercise a tool/detector/validator end to end with no network,
    no model, and no Docker::

        result, events = await agent_runner(
            [{"tool": "http_fetch", "args": {"url": "http://x/"}},
             {"text": '{"ok": true}', "finish": "stop"}],
            tools={"http_fetch": make_fetch_tool(text="<html>..</html>")},
        )

    Each call builds a fresh isolated registry + bus and tears the bus down, so
    runs never bleed into one another."""

    async def run(
        moves: list[dict],
        *,
        tools: dict[str, Any] | None = None,
        detectors: dict[str, Any] | None = None,
        validators: dict[str, Any] | None = None,
        query: str = "q",
        spec: Any = None,
        containment: str = "audit",
    ) -> tuple[Any, list[Any]]:
        from zu_core.bus import EventBus
        from zu_core.contracts import TaskSpec
        from zu_core.loop import run_task

        provider = scripted_provider(moves)
        registry = registry_with(tools=tools, detectors=detectors, validators=validators)
        bus = EventBus()
        task = spec if spec is not None else TaskSpec(query=query)
        try:
            result = await run_task(task, provider, registry, bus, containment=containment)
            return result, await bus.query()
        finally:
            await bus.aclose()

    return run
