"""End-to-end: build an agent (a custom tool behind the port + the default
validators) and run it. Proves the whole arc — fetch → extract → schema +
grounding → result — and, crucially, that the **default** validators refuse a
fabricated value instead of returning it as success.

This is the regression for "how can it pass with a made-up answer": with grounding
(now a default), it can't.
"""

from __future__ import annotations

from zu_core.bus import EventBus
from zu_core.contracts import Status, TaskSpec
from zu_core.loop import run_task
from zu_core.ports import CAP_NET, EGRESS_OPEN
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider
from zu_validators.grounding import GroundingValidator
from zu_validators.schema import SchemaValidator

from zu_cli.config import PluginsConfig

_PAGE = (
    '<html><body><h1 class="title">Aurora Desk Lamp</h1>'
    '<span class="price">$48.50</span></body></html>'
)

_SCHEMA = {
    "type": "object",
    "properties": {"name": {"type": "string"}, "price": {"type": "string"}},
    "required": ["name", "price"],
}


class _ProductFetch:
    """A custom tool (a real plugin behind the Tool port) serving a product page."""

    name = "product_fetch"
    tier = 1
    schema = {"name": "product_fetch", "description": "Fetch a product page.",
              "parameters": {"type": "object", "properties": {"url": {"type": "string"}},
                             "required": ["url"]}}
    prompt_fragment = "product_fetch(url): fetch a product page's HTML."
    capabilities = frozenset({CAP_NET})
    egress = frozenset({EGRESS_OPEN})

    async def __call__(self, ctx, url: str) -> dict:
        return {"status": 200, "html": _PAGE, "url": url}


def _registry() -> Registry:
    reg = Registry()
    reg.register("tools", "product_fetch", _ProductFetch())
    reg.register("validators", "schema", SchemaValidator())
    reg.register("validators", "grounding", GroundingValidator())
    return reg


async def _run(final_answer: str):
    provider = ScriptedProvider.from_moves([
        {"tool": "product_fetch", "args": {"url": "https://shop.example/p/2207"}},
        {"text": final_answer, "finish": "stop"},
    ])
    spec = TaskSpec(query="Extract the product name and price.",
                    target="https://shop.example/p/2207", output_schema=_SCHEMA)
    return await run_task(spec, provider, _registry(), EventBus())


def test_grounding_is_a_default_validator():
    # The fix for "a fabricated answer returned as success": the safe default.
    assert PluginsConfig().validators == ["schema", "grounding"]


async def test_grounded_extraction_succeeds():
    # The values are actually on the fetched page → schema + grounding pass.
    result = await _run('{"name": "Aurora Desk Lamp", "price": "$48.50"}')
    assert result.status is Status.SUCCESS
    assert result.value == {"name": "Aurora Desk Lamp", "price": "$48.50"}


async def test_fabricated_value_is_refused_not_returned():
    # $99.99 is NOT on the page → grounding refuses it; the run does not succeed.
    result = await _run('{"name": "Aurora Desk Lamp", "price": "$99.99"}')
    assert result.status is not Status.SUCCESS
