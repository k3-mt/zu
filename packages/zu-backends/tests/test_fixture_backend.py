"""FixtureBackend: the file-driven, URL-keyed SandboxBackend for offline tier-2
replay. It must satisfy the SandboxBackend Protocol, replay the recorded render
observation for a known URL, and return a loud (404-shaped) miss for an unknown one
so an unrecorded URL fails rather than silently passing."""

from __future__ import annotations

from zu_backends.fixture_backend import FixtureBackend
from zu_core.ports import SandboxBackend, ToolCall


def test_satisfies_sandbox_backend_protocol() -> None:
    # runtime_checkable Protocol: the structural shape (launch/exec/destroy) holds.
    assert isinstance(FixtureBackend({}), SandboxBackend)


async def test_replays_recorded_observation_for_known_url() -> None:
    url = "https://shop.example/p/acme-widget"
    backend = FixtureBackend({url: {"status": 200, "html": "<h1>Acme Widget</h1>"}})

    sandbox = await backend.launch({"image": "x", "tier": 2})
    obs = await backend.exec(sandbox, ToolCall(name="render_dom", args={"url": url}))
    await backend.destroy(sandbox)

    assert obs == {"status": 200, "html": "<h1>Acme Widget</h1>", "url": url}


async def test_unknown_url_is_a_loud_miss() -> None:
    backend = FixtureBackend({"https://known": {"status": 200, "html": "ok"}})
    obs = await backend.exec(None, ToolCall(name="render_dom", args={"url": "https://unknown"}))
    assert obs == {"status": 404, "html": "", "url": "https://unknown"}


async def test_status_defaults_to_200() -> None:
    backend = FixtureBackend({"https://x": {"html": "body"}})
    obs = await backend.exec(None, ToolCall(name="render_dom", args={"url": "https://x"}))
    assert obs["status"] == 200
