"""render_dom — the tier-2 browser tool, against a fake SandboxBackend.

render_dom never runs a browser itself; it leases one through the
SandboxBackend port. These tests inject a fake backend so the tool's contract —
normalise the observation, and always tear the sandbox down — is proven with
no Docker and no browser.
"""

from __future__ import annotations

import pytest

from zu_core.ports import ToolCall
from zu_tools.render import RenderDom

_RENDERED = "<html><body><h1>Rendered</h1></body></html>"


class _FakeBackend:
    def __init__(self, *, exec_raises: bool = False) -> None:
        self.exec_raises = exec_raises
        self.launched: list[dict] = []
        self.destroyed = 0

    async def launch(self, spec: dict):
        self.launched.append(spec)
        return {"id": "sbx", "spec": spec}

    async def exec(self, sandbox, call: ToolCall) -> dict:
        if self.exec_raises:
            raise RuntimeError("render blew up")
        return {"status": 200, "html": _RENDERED, "url": call.args["url"]}

    async def destroy(self, sandbox) -> None:
        self.destroyed += 1


async def test_render_returns_normalised_observation() -> None:
    backend = _FakeBackend()
    obs = await RenderDom(backend=backend).__call__(ctx=None, url="http://spa.test/")
    assert obs == {
        "status": 200,
        "html": _RENDERED,
        "url": "http://spa.test/",
        "rendered": True,
    }
    assert backend.launched[0]["tier"] == 2
    assert backend.destroyed == 1


async def test_sandbox_is_destroyed_even_when_render_raises() -> None:
    # A browser container must never leak: destroy runs even if exec throws.
    backend = _FakeBackend(exec_raises=True)
    with pytest.raises(RuntimeError):
        await RenderDom(backend=backend).__call__(ctx=None, url="http://spa.test/")
    assert backend.destroyed == 1


async def test_render_requests_network_egress() -> None:
    # Regression: the browser must be granted egress, or it cannot fetch the
    # page it is asked to render. render_dom must request network in its spec.
    backend = _FakeBackend()
    await RenderDom(backend=backend).__call__(ctx=None, url="http://spa.test/")
    assert backend.launched[0].get("network") is True


def test_render_dom_is_tier_2() -> None:
    # The attribute the loop's ladder reads to withhold it until escalation.
    assert RenderDom().tier == 2
