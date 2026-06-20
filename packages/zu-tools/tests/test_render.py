"""render_dom — the tier-2 browser tool, against a fake SandboxBackend.

render_dom never runs a browser itself; it leases one through the
SandboxBackend port. These tests inject a fake backend so the tool's contract —
normalise the observation, and always tear the sandbox down — is proven with
no Docker and no browser.
"""

from __future__ import annotations

import pytest

from zu_core.ports import ToolCall
from zu_testing import FakeSandboxBackend
from zu_tools.net import BlockedURLError
from zu_tools.render import RenderDom

_RENDERED = "<html><body><h1>Rendered</h1></body></html>"


def _backend(**kw):
    """The shared daemon-free sandbox backend, pre-loaded with the saved page."""
    return FakeSandboxBackend(rendered=_RENDERED, **kw)


async def test_render_returns_normalised_observation() -> None:
    backend = _backend()
    obs = await RenderDom(backend=backend, allow_private=True).__call__(ctx=None, url="http://spa.test/")
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
    backend = _backend(exec_raises=True)
    with pytest.raises(RuntimeError):
        await RenderDom(backend=backend, allow_private=True).__call__(ctx=None, url="http://spa.test/")
    assert backend.destroyed == 1


async def test_render_requests_network_egress() -> None:
    # Regression: the browser must be granted egress, or it cannot fetch the
    # page it is asked to render. render_dom must request network in its spec.
    backend = _backend()
    await RenderDom(backend=backend, allow_private=True).__call__(ctx=None, url="http://spa.test/")
    assert backend.launched[0].get("network") is True


async def test_render_applies_ssrf_guard_before_leasing_a_sandbox() -> None:
    # Escalating to tier 2 must not bypass the SSRF backstop: an internal URL is
    # refused BEFORE a browser is leased (no launch, no leak), exactly as tier 1.
    backend = _backend()
    with pytest.raises(BlockedURLError):
        await RenderDom(backend=backend, allow_private=False).__call__(
            ctx=None, url="http://169.254.169.254/latest/meta-data/"
        )
    assert backend.launched == []
    assert backend.destroyed == 0


async def test_render_pins_target_dns_to_validated_ip() -> None:
    # Tier-2 DNS pin: the validated target IP is passed as extra_hosts so the
    # browser cannot be rebound to an internal address. An IP-literal URL needs
    # no DNS, so this runs offline.
    backend = _backend()
    await RenderDom(backend=backend, allow_private=False).__call__(
        ctx=None, url="http://93.184.216.34/page"
    )
    assert backend.launched[0]["extra_hosts"] == {"93.184.216.34": "93.184.216.34"}


async def test_render_threads_viewport_into_the_tool_call() -> None:
    # A requested viewport reaches the backend exec (responsive pages need width).
    captured: dict = {}

    class _Capture(FakeSandboxBackend):
        async def exec(self, sandbox, call: ToolCall) -> dict:
            captured.update(call.args)
            return {"status": 200, "html": _RENDERED, "url": call.args["url"]}

    await RenderDom(backend=_Capture(rendered=_RENDERED), allow_private=True).__call__(
        ctx=None, url="http://spa.test/", width=375, height=812
    )
    assert captured["width"] == 375 and captured["height"] == 812


async def test_render_forwards_wait_and_actions_to_the_backend() -> None:
    # Wait/reveal params the model reasons out reach the backend exec as-is — no
    # site logic in the tool, just generic pass-through of what the model asked for.
    backend = _backend()
    actions = [{"click": "text=Next"}, {"click": "text=Dog"}, {"wait_for": "text=Choose a time"}]
    await RenderDom(backend=backend, allow_private=True).__call__(
        ctx=None, url="http://spa.test/",
        wait_until="networkidle", wait_for=".slots", wait_ms=2000, actions=actions,
    )
    call = backend.exec_calls[0]
    assert call.args["wait_until"] == "networkidle"
    assert call.args["wait_for"] == ".slots"
    assert call.args["wait_ms"] == 2000
    assert call.args["actions"] == actions


async def test_render_capture_network_forwarded_and_surfaced() -> None:
    # capture_network reaches the backend, and the captured JSON bodies come back
    # as a groundable `content` key + structured `network` — the event-driven,
    # robust way to read a widget's data instead of scraping the DOM.
    class _Net(FakeSandboxBackend):
        async def exec(self, sandbox, call: ToolCall) -> dict:
            assert call.args.get("capture_network") is True
            return {
                "status": 200, "html": "<p>x</p>", "url": call.args["url"], "text": "visible",
                "content": '# https://api/slots (200)\n{"available_dates":["2026-06-24"]}',
                "network": [{"url": "https://api/slots", "status": 200,
                             "content_type": "application/json", "body": '{"available_dates":["2026-06-24"]}'}],
            }

    out = await RenderDom(backend=_Net(rendered="x"), allow_private=True).__call__(
        ctx=None, url="http://spa.test/", capture_network=True
    )
    assert "2026-06-24" in out["content"]                 # groundable captured data
    assert out["network"][0]["url"] == "https://api/slots"
    assert out["text"] == "visible"


def test_render_dom_is_tier_2() -> None:
    # The attribute the loop's ladder reads to withhold it until escalation.
    assert RenderDom().tier == 2
