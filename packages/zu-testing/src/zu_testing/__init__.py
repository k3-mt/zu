"""zu-testing — the shared test kit for Zu and its plugins.

Importable doubles + factories (use directly), plus pytest fixtures and a plugin
(auto-loaded; markers ``live``/``docker`` and flags ``--run-live``/``--run-docker``).

    from zu_testing import FakeSandboxBackend, mock_transport, scripted_config

    async def test_my_tool(agent_runner, make_fetch_tool):   # fixtures, no import
        result, events = await agent_runner(
            [{"tool": "my_tool", "args": {}}, {"text": "{}", "finish": "stop"}],
            tools={"my_tool": MyTool()},
        )
"""

from __future__ import annotations

from .doubles import (
    ExplodingSink,
    FakeSandboxBackend,
    FakeSink,
    mock_transport,
)
from .factories import (
    fetch_tool,
    registry_with,
    scripted_config,
    scripted_provider,
    search_tool,
)

__all__ = [
    "ExplodingSink",
    "FakeSandboxBackend",
    "FakeSink",
    "mock_transport",
    "fetch_tool",
    "search_tool",
    "registry_with",
    "scripted_config",
    "scripted_provider",
]
