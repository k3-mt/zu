"""End-to-end: build an agent and run it against a REAL HTML page.

The page is a real saved file (``fixtures/product.html``) fetched through the
**real** ``http_fetch`` tool over an ``httpx.MockTransport`` — the same offline
fixture pattern ``zu-core``'s ``test_loop`` uses — so this exercises the actual
fetch path (SSRF guard, byte cap, decoding) against real HTML, not a stub. The
extracted values are then held to the **default** validators (schema + grounding):
a value present on the page is returned; a fabricated one is refused, not returned
as success. Regression for "how can it pass with a made-up answer".

A live variant (``test_live_*``) fetches an actual URL and is skipped unless
``ZU_E2E_LIVE=1`` (it needs outbound network), mirroring the repo's opt-in
live-call convention.
"""

from __future__ import annotations

import os
from pathlib import Path

import httpx
import pytest

from zu_core.bus import EventBus
from zu_core.contracts import Status, TaskSpec
from zu_core.loop import run_task
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider
from zu_tools.fetch import HttpFetch
from zu_validators.grounding import GroundingValidator
from zu_validators.schema import SchemaValidator

from zu_cli.config import PluginsConfig

_PAGE_HTML = (Path(__file__).parent / "fixtures" / "product.html").read_text(encoding="utf-8")
_URL = "https://brewterra.example/equipment/hv60-ceramic"

# Values that are genuinely present in fixtures/product.html.
_REAL_NAME = "Hario V60 Ceramic Coffee Dripper"
_REAL_PRICE = "$23.00"

_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "price": {"type": "string"}},
    "required": ["name", "price"],
}


def _real_fetch() -> HttpFetch:
    """The real http_fetch tool, but served the saved page off a MockTransport —
    no network, real fetch logic. allow_private skips DNS (the transport answers)."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_PAGE_HTML)

    return HttpFetch(allow_private=True, transport=httpx.MockTransport(handler))


def _registry(fetch: HttpFetch) -> Registry:
    reg = Registry()
    reg.register("tools", "http_fetch", fetch)
    reg.register("validators", "schema", SchemaValidator())
    reg.register("validators", "grounding", GroundingValidator())
    return reg


async def _run(final_answer: str, fetch: HttpFetch | None = None):
    provider = ScriptedProvider.from_moves([
        {"tool": "http_fetch", "args": {"url": _URL}},
        {"text": final_answer, "finish": "stop"},
    ])
    spec = TaskSpec(query="Extract the product name and price.", target=_URL, output_schema=_SCHEMA)
    return await run_task(spec, provider, _registry(fetch or _real_fetch()), EventBus())


def test_the_fixture_is_a_real_page_with_the_values():
    # Sanity: the saved HTML really contains what we'll claim to extract.
    assert "<!doctype html>" in _PAGE_HTML.lower()
    assert _REAL_NAME in _PAGE_HTML and _REAL_PRICE in _PAGE_HTML


def test_grounding_is_a_default_validator():
    assert PluginsConfig().validators == ["schema", "grounding"]


async def test_grounded_extraction_from_the_real_page_succeeds():
    result = await _run(f'{{"name": "{_REAL_NAME}", "price": "{_REAL_PRICE}"}}')
    assert result.status is Status.SUCCESS
    assert result.value == {"name": _REAL_NAME, "price": _REAL_PRICE}


async def test_fabricated_value_is_refused_not_returned():
    # $99.99 is not on the page → grounding refuses it; the run does not succeed.
    result = await _run(f'{{"name": "{_REAL_NAME}", "price": "$99.99"}}')
    assert result.status is not Status.SUCCESS


@pytest.mark.skipif(os.environ.get("ZU_E2E_LIVE") != "1", reason="needs network; set ZU_E2E_LIVE=1")
async def test_live_fetch_of_a_real_website_grounds():
    # The real tool against a real URL over real network (opt-in). example.com is
    # stable and its page contains the heading "Example Domain".
    reg = _registry(HttpFetch())
    provider = ScriptedProvider.from_moves([
        {"tool": "http_fetch", "args": {"url": "https://example.com/"}},
        {"text": '{"heading": "Example Domain"}', "finish": "stop"},
    ])
    spec = TaskSpec(
        query="Extract the page heading.", target="https://example.com/",
        output_schema={"type": "object", "properties": {"heading": {"type": "string"}},
                       "required": ["heading"]},
    )
    result = await run_task(spec, provider, reg, EventBus())
    assert result.status is Status.SUCCESS
    assert result.value == {"heading": "Example Domain"}
