"""The demos behind ``zu demo`` — shipped in the package so they run after a
pip install, no repo clone needed.

Two demo types, selectable with ``--type``:

* **escalation** (default) — the killer arc: an agent tries the cheap HTTP tier,
  **fails on a JavaScript page**, a *detector* (not the model) **escalates to a
  browser**, returns the product, and the answer is **validated** against what
  the run fetched. Uses the web tools (the ``[web]``/``[demo]`` extra); the
  network and browser are fixtured, so no real site and no Docker are involved.
* **minimal** — the smallest real loop: a model answers a question as JSON,
  validated against a schema. No tools, no network — so it runs on the **bare
  base install**, with the fake model or a real one.

Both run offline by default (a deterministic fake model). Pass a provider, a
model, and your key to watch a real model do it. We never ship or require a key.
"""

from __future__ import annotations

from typing import Any

from zu_core.bus import EventBus
from zu_core.contracts import Result, Status, TaskSpec
from zu_core.loop import run_task
from zu_core.ports import ModelProvider, ToolCall
from zu_core.registry import Registry

# Plugin packages are imported lazily inside the functions below: the escalation
# demo needs the web tools (zu-tools), an opt-in extra, so this module must still
# import on the lean base. ``ensure_web_tools`` turns a missing-tools install
# into a clear, actionable message rather than an ImportError mid-run.

_WEB_HINT = "the escalation demo needs the web tools — install them with: pip install 'zu-runtime[demo]'"


def ensure_web_tools() -> None:
    """Raise a clear, actionable error if the web tools aren't installed."""
    try:
        import zu_tools  # noqa: F401
    except ModuleNotFoundError as exc:
        raise RuntimeError(_WEB_HINT) from exc


# --- the escalation demo (web) -----------------------------------------------

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

_ESCALATION_TASK = TaskSpec(
    query="Extract the product name and price.",
    target=_URL,
    max_tier=2,
    output_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}, "price": {"type": "string"}},
        "required": ["name", "price"],
    },
)

# --- the minimal demo (no tools, runs on the bare base) ----------------------

_MINIMAL_TASK = TaskSpec(
    query="Reply with the capital of France as a JSON object: {\"capital\": ...}.",
    max_tier=1,
    output_schema={
        "type": "object",
        "properties": {"capital": {"type": "string"}},
        "required": ["capital"],
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


def _shell_fetch() -> Any:
    import httpx
    from zu_tools.fetch import HttpFetch

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_SHELL)

    return HttpFetch(allow_private=True, transport=httpx.MockTransport(handler))


def _escalation_registry() -> tuple[Registry, _FixtureBrowser]:
    """The real built-ins (only the network and browser are fixtured): tier-1
    http_fetch, tier-2 render_dom, all four detectors, both validators."""
    ensure_web_tools()
    from zu_detectors.bot_wall import BotWallDetector
    from zu_detectors.empty import EmptyDetector
    from zu_detectors.error import ErrorDetector
    from zu_detectors.js_shell import JsShellDetector
    from zu_tools.parse import HtmlParse
    from zu_tools.render import RenderDom
    from zu_validators.grounding import GroundingValidator
    from zu_validators.schema import SchemaValidator

    backend = _FixtureBrowser(_RENDERED)
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
    return reg, backend


def _minimal_registry() -> tuple[Registry, None]:
    """No tools, no network — just the schema validator. Runs on the bare base."""
    from zu_validators.schema import SchemaValidator

    reg = Registry()
    reg.register("validators", "schema", SchemaValidator())
    return reg, None


def _escalation_scripted() -> Any:
    from zu_providers.scripted import ScriptedProvider

    return ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": _URL}},   # tier 1 -> JS shell
            {"tool": "render_dom", "args": {"url": _URL}},   # tier 2 -> real DOM
            {"text": '{"name": "Acme Widget", "price": "$9.00"}', "finish": "stop"},
        ]
    )


def _minimal_scripted() -> Any:
    from zu_providers.scripted import ScriptedProvider

    return ScriptedProvider.from_moves([{"text": '{"capital": "Paris"}', "finish": "stop"}])


# The demo catalogue: each type names its task, registry/provider builders, the
# header title, whether it needs the web extra, and the closing blurb.
DEMOS: dict[str, dict[str, Any]] = {
    "escalation": {
        "task": _ESCALATION_TASK,
        "registry": _escalation_registry,
        "scripted": _escalation_scripted,
        "needs_web": True,
        "title": "killer demo — fetch, fail on JavaScript, escalate, validate",
        "blurb": (
            "Three pillars in one run: a detector (not the model) escalated to a "
            "browser, the\nresult is grounded in the rendered DOM, and the whole "
            "run is a queryable log."
        ),
    },
    "minimal": {
        "task": _MINIMAL_TASK,
        "registry": _minimal_registry,
        "scripted": _minimal_scripted,
        "needs_web": False,
        "title": "minimal demo — a model answers, schema-validated (no tools, no network)",
        "blurb": (
            "The smallest real loop: the model produced a JSON answer and the "
            "schema validator\nconfirmed its shape — all captured as a queryable "
            "event log. Runs on the bare base."
        ),
    },
}

DEMO_TYPES = tuple(DEMOS)


# --- running + narration -----------------------------------------------------


_NARRATION = {
    "harness.task.started": "📋 task started",
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


async def run_arc(
    provider: ModelProvider, kind: str = "escalation"
) -> tuple[Result, EventBus, Any]:
    """Drive the chosen demo; return the result, the event bus (the log), and the
    fixture browser (escalation) or None (minimal)."""
    registry, backend = DEMOS[kind]["registry"]()
    bus = EventBus()
    result = await run_task(DEMOS[kind]["task"], provider, registry, bus)
    return result, bus, backend


async def run_demo(
    provider: ModelProvider, provider_label: str = "scripted", kind: str = "escalation"
) -> int:
    """Run the chosen demo and print the narrated timeline + result. Returns a
    process exit code (0 success, 1 otherwise)."""
    meta = DEMOS[kind]
    task = meta["task"]
    model = getattr(provider, "model", None) or provider_label

    print("=" * 72)
    print(f"Zu · {meta['title']}")
    print("=" * 72)
    print(f"type     : {kind}")
    print(f"task     : {task.query}")
    if task.target:
        print(f"target   : {task.target}")
    print(f"provider : {provider_label}  model: {model}")
    print("-" * 72)

    try:
        result, bus, backend = await run_arc(provider, kind)
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
    if backend is not None:
        print(
            f"provenance: {await bus.count()} events recorded · "
            f"tier-2 browser leased {len(backend.launched)}×, torn down {backend.destroyed}×"
        )
    else:
        print(f"provenance: {await bus.count()} events recorded")

    if result.status is Status.SUCCESS:
        print(f"\n{meta['blurb']}\nSwap the model with one config line — see the quickstart.")
        return 0
    print("\nThe run did not succeed — inspect the timeline above and the event log.")
    return 1


def build_provider(
    provider: str,
    model: str | None,
    api_key: str | None,
    api_key_env: str | None,
    base_url_env: str | None,
    kind: str = "escalation",
) -> tuple[ModelProvider, str]:
    """Pick the demo's model: ``scripted`` (default, offline) or any real provider
    selected the same way ``zu run`` does — proving one config surface."""
    if provider == "scripted":
        return DEMOS[kind]["scripted"](), "scripted"
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
