"""The killer demo, shipped in the package so ``zu demo`` runs after a pip
install — no repo clone needed.

It runs the whole arc in one go: an agent tries the cheap HTTP tier, **fails on
a JavaScript page**, a *detector* (not the model) **escalates to a browser**,
returns the product, and the answer is **validated** against what the run
actually fetched — with the run queryable afterward as an event log.

Two modes, both initialised straight away:

* **offline (default)** — a fake model and saved fixtures: deterministic, no API
  key, no network, no Docker. Works the instant the package is installed.
* **real model** — pass a provider + model (and your key, via ``--api-key`` or an
  env var). The page is still fixtured, so no Docker is needed; you watch a live
  model make the same escalation decision. We never ship or require a key.
"""

from __future__ import annotations

from typing import Any

import httpx

from zu_core.bus import EventBus
from zu_core.contracts import Result, Status, TaskSpec
from zu_core.loop import run_task
from zu_core.ports import ModelProvider, ToolCall
from zu_core.registry import Registry
from zu_detectors.bot_wall import BotWallDetector
from zu_detectors.empty import EmptyDetector
from zu_detectors.error import ErrorDetector
from zu_detectors.js_shell import JsShellDetector
from zu_providers.scripted import ScriptedProvider
from zu_tools.fetch import HttpFetch
from zu_tools.parse import HtmlParse
from zu_tools.render import RenderDom
from zu_validators.grounding import GroundingValidator
from zu_validators.schema import SchemaValidator

# The product page, two ways. Over plain HTTP it's an empty JS shell (a mount
# point and a script, no content). A real browser runs the JS and the product
# appears.
_URL = "http://shop.test/product/123"
_SHELL = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
_RENDERED = (
    "<html><body><main>"
    "<h1>Acme Widget</h1><span class='price'>$9.00</span>"
    "</main></body></html>"
)

_TASK = TaskSpec(
    query="Extract the product name and price.",
    target=_URL,
    max_tier=2,
    output_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}, "price": {"type": "string"}},
        "required": ["name", "price"],
    },
)


class _FixtureBrowser:
    """A stand-in SandboxBackend: no Docker, no real browser — returns the saved
    rendered page. Swap in ``local-docker`` and the same code runs for real."""

    name = "fixture-browser"

    def __init__(self, rendered: str) -> None:
        self._rendered = rendered
        self.launched: list[dict] = []
        self.destroyed = 0

    async def launch(self, spec: dict) -> dict:
        self.launched.append(spec)
        return {"id": "sbx-1", "spec": spec}

    async def exec(self, sandbox: dict, call: ToolCall) -> dict:
        return {"status": 200, "html": self._rendered, "url": call.args["url"]}

    async def destroy(self, sandbox: dict) -> None:
        self.destroyed += 1


def _shell_fetch() -> HttpFetch:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_SHELL)

    return HttpFetch(allow_private=True, transport=httpx.MockTransport(handler))


def build_demo_registry(backend: _FixtureBrowser) -> Registry:
    """The real built-ins (only the network and browser are fixtured): tier-1
    http_fetch, tier-2 render_dom, all four detectors, both validators."""
    reg = Registry()
    reg.register("tools", "http_fetch", _shell_fetch())
    reg.register("tools", "html_parse", HtmlParse())
    reg.register("tools", "render_dom", RenderDom(backend=backend))
    for name, det in [
        ("empty", EmptyDetector()),
        ("error", ErrorDetector()),
        ("js-shell", JsShellDetector()),
        ("bot-wall", BotWallDetector()),
    ]:
        reg.register("detectors", name, det)
    reg.register("validators", "schema", SchemaValidator())
    reg.register("validators", "grounding", GroundingValidator())
    return reg


def scripted_arc() -> ScriptedProvider:
    """The deterministic fake model: http_fetch (a shell -> escalate), render_dom
    on the new tier, then finalise the product. The answer is grounded in the
    rendered DOM, so validation passes."""
    return ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": _URL}},
            {"tool": "render_dom", "args": {"url": _URL}},
            {"text": '{"name": "Acme Widget", "price": "$9.00"}', "finish": "stop"},
        ]
    )


async def run_arc(provider: ModelProvider) -> tuple[Result, EventBus, _FixtureBrowser]:
    """Drive the arc; return the result, the event bus (the log), and the fixture
    browser (so a caller can confirm the tier-2 lease)."""
    backend = _FixtureBrowser(_RENDERED)
    bus = EventBus()
    result = await run_task(_TASK, provider, build_demo_registry(backend), bus)
    return result, bus, backend


_NARRATION = {
    "harness.task.started": "📋 task started — try the cheap tier first",
    "harness.tool.invoked": "🔧 tool: {tool}",
    "harness.detector.fired": "🔎 detector fired: {detector} ({severity}) — {detail}",
    "harness.task.escalated": "⬆️  ESCALATE {from_tier}→{to_tier}: {reason} — climbing to a browser",
    "harness.validation.failed": "❌ validation: {detector} ({severity}) — {detail}",
    "data.record.extracted": "📦 extracted: {value}",
    "harness.task.completed": "✅ completed",
    "harness.task.terminal": "🛑 terminal: {reason}",
}


def _narrate(event_type: str, payload: dict) -> str | None:
    template = _NARRATION.get(event_type)
    if template is None:
        return None
    try:
        return template.format(**payload)
    except (KeyError, IndexError):
        return template


async def run_demo(provider: ModelProvider, provider_label: str = "scripted") -> int:
    """Run the arc and print the narrated timeline + result. Returns a process
    exit code (0 success, 1 otherwise)."""
    model = getattr(provider, "model", None) or provider_label

    print("=" * 72)
    print("Zu · killer demo — fetch, fail on JavaScript, escalate, validate")
    print("=" * 72)
    print(f"task     : {_TASK.query}")
    print(f"target   : {_TASK.target}")
    print(f"provider : {provider_label}  model: {model}")
    print("-" * 72)

    try:
        result, bus, backend = await run_arc(provider)
    except Exception as exc:  # noqa: BLE001 - a clean message beats a traceback
        print(f"\nrun failed: {type(exc).__name__}: {exc}")
        return 1

    for e in await bus.query():
        line = _narrate(e.type, e.payload)
        if line is not None:
            print(f"  {line}")

    print("-" * 72)
    print(f"RESULT   : {result.status.value}")
    if result.value is not None:
        print(f"value    : {result.value}")
    if result.reason is not None:
        print(f"reason   : {result.reason}")
    print(
        f"provenance: {await bus.count()} events recorded · "
        f"tier-2 browser leased {len(backend.launched)}×, torn down {backend.destroyed}×"
    )

    if result.status is Status.SUCCESS:
        print(
            "\nThree pillars in one run: a detector (not the model) escalated to a "
            "browser, the\nresult is grounded in the rendered DOM, and the whole "
            "run is a queryable log.\nSwap the model with one config line — see the "
            "quickstart."
        )
        return 0
    print("\nThe run did not succeed — inspect the timeline above and the event log.")
    return 1


def build_provider(
    provider: str,
    model: str | None,
    api_key: str | None,
    api_key_env: str | None,
    base_url_env: str | None,
) -> tuple[ModelProvider, str]:
    """Pick the demo's model: ``scripted`` (default, offline) or any real provider
    selected the same way ``zu run`` does — proving one config surface."""
    if provider == "scripted":
        return scripted_arc(), "scripted"
    from .config import ProviderConfig, build_provider as cfg_build_provider

    prov = cfg_build_provider(
        ProviderConfig(
            name=provider,
            model=model,
            api_key=api_key,
            api_key_env=api_key_env,
            base_url_env=base_url_env,
        )
    )
    return prov, provider
