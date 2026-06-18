"""The killer demo — the whole arc, three pillars in one run (build step 9).

Watch an agent try the cheap path, **fail on a JavaScript page**, **escalate to
a real browser** (a decision the harness makes, never the model), return
structured data, and have that data **validated against what the run actually
fetched** — with the entire run queryable afterward as an event log.

    Pillar 1 · Deterministic escalation
        A JS shell defeats tier-1 http_fetch. The js-shell *detector* — not the
        model — fires, and the loop climbs to tier-2 render_dom (a browser).
    Pillar 2 · Event-sourced provenance
        Every step is an append-only event. The escalation is a recorded climb
        (from_tier -> to_tier); the run is lossless and replayable.
    Pillar 3 · Capability-envelope + validated output
        The model may signal an action, never acquire a capability. The final
        answer must satisfy the task schema AND be grounded in fetched content.

Runs with **zero setup** by default — a fake model and saved fixtures, fully
deterministic, no API key, no network, no Docker:

    uv run python examples/killer_demo.py

Point it at a **real model** to watch a live model make the same escalation
decision. Still no Docker — the page is fixtured, so all you need is one key:

    export ANTHROPIC_API_KEY=...
    uv run python examples/killer_demo.py --provider anthropic --model claude-sonnet-4-6

    export OPENROUTER_API_KEY=...
    uv run python examples/killer_demo.py --provider openai-compatible \
        --model anthropic/claude-3.5-haiku --base-url-env OPENROUTER_BASE_URL \
        --api-key-env OPENROUTER_API_KEY

That last line is the whole "run on any model" promise: the same arc, a
different adapter, selected by config — no code change.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

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

# The product page, two ways. Over plain HTTP it is an empty JS shell — a mount
# point and a script, no real content (what a naive scraper sees). Once a real
# browser runs the JavaScript, the actual product appears.
_URL = "http://shop.test/product/123"
_SHELL = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
_RENDERED = (
    "<html><body><main>"
    "<h1>Acme Widget</h1><span class='price'>$9.00</span>"
    "</main></body></html>"
)

# What we want out of the page — the shape the result must satisfy.
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
    """A stand-in SandboxBackend: no Docker, no real browser. It returns the
    saved rendered page, freezing tier 2 the way the fake model freezes the
    model — so the escalation arc is demonstrable on any machine. Swap in
    ``local-docker`` (a real Chromium sidecar) and the same code runs for real.
    """

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
    """A real http_fetch whose network is fixtured to return the JS shell —
    `allow_private` so the localhost-looking test URL passes the SSRF guard."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_SHELL)

    return HttpFetch(allow_private=True, transport=httpx.MockTransport(handler))


def build_demo_registry(backend: _FixtureBrowser) -> Registry:
    """Exactly the plugins the killer demo needs, all the real built-ins except
    the fixtured network/browser: tier-1 http_fetch, tier-2 render_dom, the four
    detectors (js-shell drives the climb), and both validators."""
    reg = Registry()
    reg.register("tools", "http_fetch", _shell_fetch())            # tier 1
    reg.register("tools", "html_parse", HtmlParse())               # tier 1
    reg.register("tools", "render_dom", RenderDom(backend=backend))  # tier 2
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


def _scripted_arc() -> ScriptedProvider:
    """The deterministic fake model: try http_fetch (gets a shell -> the harness
    escalates), then render_dom on the new tier, then finalise the product. The
    final answer is grounded in the rendered DOM, so validation passes."""
    return ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": _URL}},   # tier 1 -> JS shell
            {"tool": "render_dom", "args": {"url": _URL}},   # tier 2 -> real DOM
            {"text": '{"name": "Acme Widget", "price": "$9.00"}', "finish": "stop"},
        ]
    )


# How each event type reads in the narrated timeline. Unlisted types are skipped
# so the story stays legible (turn bookkeeping, tool.returned summaries, etc.).
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
        return template  # a payload missing an optional field still narrates


async def run_demo(provider: ModelProvider) -> tuple[Result, EventBus, _FixtureBrowser]:
    """Drive the arc and return the result, the event bus (the queryable log),
    and the fixture browser (so a caller can confirm the tier-2 lease)."""
    backend = _FixtureBrowser(_RENDERED)
    bus = EventBus()
    result = await run_task(_TASK, provider, build_demo_registry(backend), bus)
    return result, bus, backend


def _build_provider(args: argparse.Namespace) -> ModelProvider:
    if args.provider == "scripted":
        return _scripted_arc()
    # A real model, selected exactly the way `zu run`'s config does (build step
    # 8) — so this path proves the same config surface drives a live run.
    from zu_cli.config import ProviderConfig, build_provider

    return build_provider(
        ProviderConfig(
            name=args.provider,
            model=args.model,
            api_key_env=args.api_key_env,
            base_url_env=args.base_url_env,
        )
    )


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Zu killer demo — the escalation arc.")
    p.add_argument("--provider", default="scripted", help="scripted (default) | anthropic | openai-compatible")
    p.add_argument("--model", default=None, help="model id for a real provider")
    p.add_argument("--api-key-env", default=None, help="env var holding the API key")
    p.add_argument("--base-url-env", default=None, help="env var holding the base URL (openai-compatible)")
    return p.parse_args(argv)


async def _amain(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    provider = _build_provider(args)
    model = getattr(provider, "model", None) or args.provider

    print("=" * 72)
    print("Zu · killer demo — fetch, fail on JavaScript, escalate, validate")
    print("=" * 72)
    print(f"task     : {_TASK.query}")
    print(f"target   : {_TASK.target}")
    print(f"provider : {args.provider}  model: {model}")
    print("-" * 72)

    try:
        result, bus, backend = await run_demo(provider)
    except Exception as exc:  # noqa: BLE001 - a clean message beats a traceback
        # A real provider with no key / an unreachable endpoint surfaces here
        # (the loop turns *tool* failures into observations, but a model-call
        # failure propagates). The default scripted path never hits this.
        print(f"\nrun failed: {type(exc).__name__}: {exc}", file=sys.stderr)
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
            "run is a queryable log.\nSwap the model with one line — see "
            "examples/zu.example.yaml and `zu run`."
        )
        return 0
    print("\nThe run did not succeed — inspect the timeline above and the event log.")
    return 1


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
