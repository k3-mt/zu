"""The demos behind ``zu demo`` — shipped in the package so they run after a
pip install, no repo clone needed.

The demos exist to prove **runnability**: that a freshly installed Zu actually
runs an agent against a **real model** and produces a validated result. So a real
run needs an API key — the default. (``--offline`` replays a scripted, fixtured
run instead; that proves the *wiring* for CI/self-test, not a real run, and is
labelled as such.)

Demo types (``--type``), ordered by what they require to run:

* **minimal** — a model answers a question as JSON, schema-validated. No tools,
  no network. Requires: an **API key**.
* **web** (default) — a real ``http_fetch`` of a real page + the model extracts a
  field + schema/grounding validation. This is **tier 1**: requires an **API key
  + network**, and the ``[demo]`` extra. **No Docker.**
* **escalation** — the full fetch → fail-on-JS → escalate-to-browser arc. Tier 2
  (a real browser) needs **Docker**, and the headless-Chromium image is not yet
  published — so the *real* path isn't available out of the box. Use
  ``--offline`` to watch the escalation logic with a fixtured browser.

The requirement ladder: Python → +network (tier 1) → +Docker (tier 2), plus an
API key for any real model. We never ship or require a key.
"""

from __future__ import annotations

from typing import Any

from zu_core.bus import EventBus
from zu_core.contracts import Result, Status, TaskSpec
from zu_core.loop import run_task
from zu_core.ports import ModelProvider, ToolCall
from zu_core.registry import Registry

# The web tools (zu-tools) are an opt-in extra; this module must still import on
# the lean base, so plugin packages are imported lazily inside the builders.
_WEB_HINT = "this demo needs the web tools — install them with: pip install 'zu-runtime[demo]'"


def ensure_web_tools() -> None:
    """Raise a clear, actionable error if the web tools aren't installed."""
    try:
        import zu_tools  # noqa: F401
    except ModuleNotFoundError as exc:
        raise RuntimeError(_WEB_HINT) from exc


# --- tasks -------------------------------------------------------------------

_MINIMAL_TASK = TaskSpec(
    query="Reply with the capital of France as a JSON object: {\"capital\": ...}.",
    max_tier=1,
    output_schema={
        "type": "object",
        "properties": {"capital": {"type": "string"}},
        "required": ["capital"],
    },
)

_WEB_URL = "https://example.com"
_WEB_TASK = TaskSpec(
    query=(
        "Fetch the page and extract its main heading. "
        "Return a JSON object: {\"title\": ...}."
    ),
    target=_WEB_URL,
    max_tier=1,
    output_schema={
        "type": "object",
        "properties": {"title": {"type": "string"}},
        "required": ["title"],
    },
)

# A stand-in for example.com used only by the offline self-test (no network).
_EXAMPLE_HTML = (
    "<html><head><title>Example Domain</title></head><body><main>"
    "<h1>Example Domain</h1>"
    "<p>This domain is for use in illustrative examples in documents.</p>"
    "</main></body></html>"
)

# --- the escalation arc (offline self-test; real tier-2 needs Docker + image) -

_SHELL_URL = "http://shop.test/product/123"
_SHELL = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
_RENDERED = (
    "<html><body><main><h1>Acme Widget</h1><span class='price'>$9.00</span></main></body></html>"
)
_ESCALATION_TASK = TaskSpec(
    query="Extract the product name and price.",
    target=_SHELL_URL,
    max_tier=2,
    output_schema={
        "type": "object",
        "properties": {"name": {"type": "string"}, "price": {"type": "string"}},
        "required": ["name", "price"],
    },
)


class _FixtureBrowser:
    """A stand-in SandboxBackend for the offline escalation self-test: no Docker,
    no real browser — returns the saved rendered page."""

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


# --- registries (built per type; ``offline`` fixtures the network/browser) ----


def _minimal_registry(offline: bool) -> tuple[Registry, None]:
    from zu_validators.schema import SchemaValidator

    reg = Registry()
    reg.register("validators", "schema", SchemaValidator())
    return reg, None


def _web_registry(offline: bool) -> tuple[Registry, None]:
    ensure_web_tools()
    from zu_tools.fetch import HttpFetch
    from zu_tools.parse import HtmlParse
    from zu_validators.grounding import GroundingValidator
    from zu_validators.schema import SchemaValidator

    if offline:
        import httpx

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text=_EXAMPLE_HTML)

        fetch = HttpFetch(allow_private=True, transport=httpx.MockTransport(handler))
    else:
        fetch = HttpFetch()  # real network

    reg = Registry()
    reg.register("tools", "http_fetch", fetch)
    reg.register("tools", "html_parse", HtmlParse())
    reg.register("validators", "schema", SchemaValidator())
    reg.register("validators", "grounding", GroundingValidator())
    return reg, None


def _escalation_registry(offline: bool) -> tuple[Registry, Any]:
    if not offline:
        raise RuntimeError(
            "the real escalation demo needs Docker and a published tier-2 browser image "
            "(not available yet); run it with --offline to see the escalation logic."
        )
    ensure_web_tools()
    from zu_detectors.bot_wall import BotWallDetector
    from zu_detectors.empty import EmptyDetector
    from zu_detectors.error import ErrorDetector
    from zu_detectors.js_shell import JsShellDetector
    from zu_tools.fetch import HttpFetch
    from zu_tools.parse import HtmlParse
    from zu_tools.render import RenderDom
    from zu_validators.grounding import GroundingValidator
    from zu_validators.schema import SchemaValidator
    import httpx

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_SHELL)

    backend = _FixtureBrowser(_RENDERED)
    reg = Registry()
    reg.register("tools", "http_fetch", HttpFetch(allow_private=True, transport=httpx.MockTransport(handler)))
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


# --- scripted providers (used ONLY by --offline self-test) -------------------


def _minimal_scripted() -> Any:
    from zu_providers.scripted import ScriptedProvider

    return ScriptedProvider.from_moves([{"text": '{"capital": "Paris"}', "finish": "stop"}])


def _web_scripted() -> Any:
    from zu_providers.scripted import ScriptedProvider

    return ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": _WEB_URL}},
            {"text": '{"title": "Example Domain"}', "finish": "stop"},
        ]
    )


def _escalation_scripted() -> Any:
    from zu_providers.scripted import ScriptedProvider

    return ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": _SHELL_URL}},
            {"tool": "render_dom", "args": {"url": _SHELL_URL}},
            {"text": '{"name": "Acme Widget", "price": "$9.00"}', "finish": "stop"},
        ]
    )


DEMOS: dict[str, dict[str, Any]] = {
    "minimal": {
        "task": _MINIMAL_TASK,
        "registry": _minimal_registry,
        "scripted": _minimal_scripted,
        "needs_web": False,
        "requires": "an API key (a real model)",
        "title": "minimal — a model answers, schema-validated (no tools, no network)",
    },
    "web": {
        "task": _WEB_TASK,
        "registry": _web_registry,
        "scripted": _web_scripted,
        "needs_web": True,
        "requires": "an API key + network · tier 1, no Docker",
        "title": "web — fetch a real page, extract a field, validate (tier 1)",
    },
    "escalation": {
        "task": _ESCALATION_TASK,
        "registry": _escalation_registry,
        "scripted": _escalation_scripted,
        "needs_web": True,
        "requires": "an API key + Docker (tier-2 browser) — real path not yet available; use --offline",
        "title": "escalation — fetch, fail on JS, escalate to a browser, validate (tier 2)",
    },
}

DEMO_TYPES = tuple(DEMOS)


# --- running ------------------------------------------------------------------


async def run_arc(
    provider: ModelProvider, kind: str = "web", offline: bool = False, bus: EventBus | None = None
) -> tuple[Result, EventBus, Any]:
    """Drive the chosen demo; return the result, the event bus, and the fixture
    browser (escalation) or None. Pass a ``bus`` with a subscriber attached to
    watch the run live."""
    registry, backend = DEMOS[kind]["registry"](offline)
    bus = bus or EventBus()
    result = await run_task(DEMOS[kind]["task"], provider, registry, bus)
    return result, bus, backend


async def run_demo(
    provider: ModelProvider,
    provider_label: str = "scripted",
    kind: str = "web",
    offline: bool = False,
) -> int:
    """Run the chosen demo and print the narrated timeline + result. Returns a
    process exit code (0 success, 1 otherwise)."""
    meta = DEMOS[kind]
    task = meta["task"]
    # Only a real model id, never the provider name standing in for one — a
    # scripted/offline provider has no model, and "model=scripted" conflates the
    # two. None here means the provider line below shows the provider alone.
    model = getattr(provider, "model", None)

    print("=" * 72)
    print(f"Zu · demo: {meta['title']}")
    print("=" * 72)
    print(f"type     : {kind}{'  (offline self-test — wiring only, not a real run)' if offline else ''}")
    print(f"requires : {meta['requires']}")
    print(f"task     : {task.query}")
    if task.target:
        print(f"target   : {task.target}")
    print(f"provider : {provider_label}" + (f"  model: {model}" if model else ""))
    print("-" * 72)

    # Watch the run live — the train of thought, tools, and escalations stream
    # to the console as the loop runs (the same trace `zu run` shows).
    from .trace import live_printer

    bus = EventBus()
    bus.subscribe(live_printer())
    try:
        result, bus, backend = await run_arc(provider, kind, offline, bus=bus)
    except Exception as exc:  # noqa: BLE001 - a clean message beats a traceback
        print(f"\nrun failed: {type(exc).__name__}: {exc}")
        return 1

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
        proof = "wiring verified (offline)" if offline else "a real model ran end to end"
        print(f"\n{proof}: validated result + a queryable event log. See the quickstart.")
        return 0
    print("\nThe run did not succeed — inspect the timeline above and the event log.")
    return 1


def build_provider(
    provider: str | None,
    model: str | None,
    api_key: str | None,
    api_key_env: str | None,
    base_url_env: str | None,
    kind: str = "web",
    offline: bool = False,
) -> tuple[ModelProvider, str]:
    """Pick the demo's model. ``--offline`` uses the scripted self-test provider;
    otherwise a real provider is built the same way ``zu run`` does (proving one
    config surface), defaulting to Anthropic when only a model is given."""
    if offline:
        return DEMOS[kind]["scripted"](), "scripted"
    from .config import ProviderConfig, build_provider as cfg_build_provider

    prov = cfg_build_provider(
        ProviderConfig(
            name=provider or "anthropic",
            model=model,
            api_key=api_key,
            api_key_env=api_key_env,
            base_url_env=base_url_env,
        )
    )
    return prov, (provider or "anthropic")
