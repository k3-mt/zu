"""Build steps 4–5 — the interpreter loop, tier-1 tools, and the escalation step.

Proves the loop drives provider -> tool -> observation -> finalise with the
ScriptedProvider (fake model) and a fixtured page, **deterministically**: the
same Result and the same sequence of event types every run, with no network.
Budgets, tool-error isolation, and the detector checkpoint are exercised too.

Step 5 adds the escalation ladder: a JS-shell page makes tier 1 escalate, the
loop climbs to tier 2, and the same job succeeds with a (scripted) browser —
no Docker, no real browser, the same fixture discipline as everywhere else.
"""

from __future__ import annotations

import httpx
import pytest

from zu_core.bus import EventBus
from zu_core.contracts import Budget, Status, TaskSpec
from zu_core.loop import run_task
from zu_core.ports import Finish, ModelResponse, Scope, Severity, ToolCall, Verdict
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider
from zu_tools.fetch import HttpFetch
from zu_tools.parse import HtmlParse

_PAGE = "<html><body><h1>Acme Widget</h1><span class='price'>$9.00</span></body></html>"


def _fetch_fixture() -> HttpFetch:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_PAGE)

    # allow_private skips DNS; the MockTransport returns the saved page — no net.
    return HttpFetch(allow_private=True, transport=httpx.MockTransport(handler))


def _registry_with_tools() -> Registry:
    reg = Registry()
    reg.register("tools", "http_fetch", _fetch_fixture())
    reg.register("tools", "html_parse", HtmlParse())
    return reg


async def test_loop_fetch_then_finalise() -> None:
    provider = ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": "http://example.test/"}},
            {"text": '{"title": "Acme Widget", "price": "$9.00"}', "finish": "stop"},
        ]
    )
    bus = EventBus()
    spec = TaskSpec(query="get the title and price")
    result = await run_task(spec, provider, _registry_with_tools(), bus)

    assert result.status == Status.SUCCESS
    assert result.value == {"title": "Acme Widget", "price": "$9.00"}

    types = [e.type for e in await bus.query()]
    assert types == [
        "harness.task.started",
        "harness.turn.started",
        "harness.turn.completed",  # per-call usage recorded right after the model call
        "harness.tool.invoked",
        "data.source.fetched",  # the fetch carried html -> a data event
        "harness.tool.returned",
        "harness.turn.started",
        "harness.turn.completed",
        "data.record.extracted",
        "harness.task.completed",
    ]


async def test_loop_is_deterministic() -> None:
    def make():
        return ScriptedProvider.from_moves(
            [
                {"tool": "http_fetch", "args": {"url": "http://example.test/"}},
                {"text": '{"title": "Acme Widget"}', "finish": "stop"},
            ]
        )

    spec = TaskSpec(query="title please")
    r1 = await run_task(spec, make(), _registry_with_tools(), EventBus())
    r2 = await run_task(spec, make(), _registry_with_tools(), EventBus())
    assert r1 == r2  # identical Result every run


async def test_budget_max_steps_is_terminal() -> None:
    # A model that only ever calls tools, with a 2-step budget, must stop.
    provider = ScriptedProvider.from_moves(
        [{"tool": "http_fetch", "args": {"url": "http://example.test/"}}] * 5
    )
    spec = TaskSpec(query="loops forever", budget=Budget(max_steps=2))
    result = await run_task(spec, provider, _registry_with_tools(), EventBus())
    assert result.status == Status.TERMINAL
    assert result.reason == "budget:max_steps"


async def test_tool_error_becomes_observation_not_crash() -> None:
    # http_fetch with the SSRF guard active refuses an internal URL; the loop
    # captures that as an observation and the model still finalises.
    reg = Registry()
    reg.register("tools", "http_fetch", HttpFetch(allow_private=False))
    provider = ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": "http://169.254.169.254/"}},
            {"text": '{"ok": false}', "finish": "stop"},
        ]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="try metadata"), provider, reg, bus)
    assert result.status == Status.SUCCESS  # the loop didn't crash
    returned = [e for e in await bus.query() if e.type == "harness.tool.returned"]
    assert "error" in returned[0].payload["observation"]


async def test_no_answer_is_terminal() -> None:
    # Out of script -> ScriptedProvider returns STOP with no text -> terminal.
    provider = ScriptedProvider.from_moves([])
    result = await run_task(TaskSpec(query="silence"), provider, Registry(), EventBus())
    assert result.status == Status.TERMINAL
    assert result.reason == "model finalised with no answer"


class _RecordingProvider:
    """Wraps the ScriptedProvider to capture the messages it's sent each turn."""

    def __init__(self, moves: list[dict]) -> None:
        self._inner = ScriptedProvider.from_moves(moves)
        self.capabilities = self._inner.capabilities
        self.seen: list[list[dict]] = []
        self.tools_seen: list[list[str]] = []  # tool names offered each turn

    async def complete(self, req):
        self.seen.append([dict(m) for m in req.messages])
        self.tools_seen.append([t["name"] for t in req.tools])
        return await self._inner.complete(req)


async def test_message_format_is_stable() -> None:
    # Pins the neutral message shape the step-7 provider adapters translate.
    provider = _RecordingProvider(
        [
            {"tool": "http_fetch", "args": {"url": "http://example.test/"}},
            {"text": "{}", "finish": "stop"},
        ]
    )
    await run_task(TaskSpec(query="get the title"), provider, _registry_with_tools(), EventBus())

    first = provider.seen[0]
    assert [m["role"] for m in first] == ["system", "user"]
    assert first[1]["content"] == "get the title"

    second = provider.seen[1]  # after the tool ran
    assert [m["role"] for m in second] == ["system", "user", "assistant", "tool"]
    assert second[2]["tool_calls"][0]["name"] == "http_fetch"
    assert second[3]["name"] == "http_fetch"


async def test_per_turn_tool_call_cap() -> None:
    # A single response with more tool calls than the budget allows is terminal.
    burst = ModelResponse(
        tool_calls=[ToolCall(name="http_fetch", args={"url": "http://x.test/"}) for _ in range(5)],
        finish=Finish.TOOL_CALLS,
    )
    provider = ScriptedProvider([burst])
    spec = TaskSpec(query="flood", budget=Budget(max_tool_calls=2))
    result = await run_task(spec, provider, _registry_with_tools(), EventBus())
    assert result.status == Status.TERMINAL
    assert result.reason == "budget:max_tool_calls"


async def test_fetched_content_stored_once() -> None:
    # The page HTML lives in data.source.fetched; tool.returned only summarises.
    provider = ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": "http://example.test/"}},
            {"text": "{}", "finish": "stop"},
        ]
    )
    bus = EventBus()
    await run_task(TaskSpec(query="x"), provider, _registry_with_tools(), bus)
    events = await bus.query()

    fetched = next(e for e in events if e.type == "data.source.fetched")
    returned = next(e for e in events if e.type == "harness.tool.returned")
    assert fetched.payload["html"] == _PAGE  # full content kept once
    assert "html" not in returned.payload["observation"]  # not duplicated
    assert returned.payload["observation"]["html_len"] == len(_PAGE)
    # source is the tool, not the constant "loop" — the filter axis is useful.
    assert fetched.source == "http_fetch"


async def test_per_turn_detector_branch() -> None:
    class PerTurnEscalate:
        name = "per-turn"
        scope = Scope.PER_TURN

        def inspect(self, ctx) -> Verdict:
            return Verdict(severity=Severity.ESCALATE, detector=self.name, detail="x")

    reg = _registry_with_tools()
    reg.register("detectors", "per-turn", PerTurnEscalate())
    provider = ScriptedProvider.from_moves(
        [{"tool": "http_fetch", "args": {"url": "http://example.test/"}}, {"text": "{}", "finish": "stop"}]
    )
    result = await run_task(TaskSpec(query="x"), provider, reg, EventBus())
    assert result.status == Status.ESCALATE
    assert result.reason == "per-turn"


async def test_validation_retry_then_success() -> None:
    # A failing validator returns RETRY; the loop feeds it back and the model
    # corrects on the next turn — exercising the ON_FINAL validation ladder.
    class NeedsFixed:
        name = "needs-fixed"

        def check(self, result, ctx):
            if "fixed" not in (result.value or {}):
                return Verdict(severity=Severity.RETRY, detector=self.name, detail="missing 'fixed'")
            return None

    reg = _registry_with_tools()
    reg.register("validators", "needs-fixed", NeedsFixed())
    provider = ScriptedProvider.from_moves(
        [
            {"text": '{"a": 1}', "finish": "stop"},  # fails validation -> RETRY
            {"text": '{"fixed": true}', "finish": "stop"},  # corrected -> passes
        ]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="x"), provider, reg, bus)
    assert result.status == Status.SUCCESS
    assert result.value == {"fixed": True}
    assert any(e.type == "harness.validation.failed" for e in await bus.query())


async def test_detector_escalate_halts_loop() -> None:
    # The detector checkpoint is where escalation is decided (step 5 wires the
    # real detectors; here we prove the mechanism fires and halts).
    class AlwaysEscalate:
        name = "always"
        scope = Scope.PER_OBSERVATION

        def inspect(self, ctx) -> Verdict:
            return Verdict(severity=Severity.ESCALATE, detector=self.name, detail="x")

    reg = _registry_with_tools()
    reg.register("detectors", "always", AlwaysEscalate())
    provider = ScriptedProvider.from_moves(
        [{"tool": "http_fetch", "args": {"url": "http://example.test/"}}, {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="x"), provider, reg, bus)
    assert result.status == Status.ESCALATE
    assert result.reason == "always"
    assert any(e.type == "harness.detector.fired" for e in await bus.query())


# --- build step 5: detectors + the escalation step + tier-2 render_dom -------

# A JS shell: a mount point plus scripts and no real content — the canonical
# tier-1 give-up signal the js-shell detector fires on.
_SHELL = '<html><body><div id="root"></div><script src="/app.js"></script></body></html>'
# What the same URL yields once a real browser runs the JavaScript.
_RENDERED = "<html><body><h1>Acme Widget</h1><span class='price'>$9.00</span></body></html>"


class _FakeBackend:
    """A scripted SandboxBackend: no Docker, no browser — it returns a saved
    rendered page, freezing tier 2 the way the ScriptedProvider freezes the
    model. Records the lifecycle so the test can assert launch/destroy ran."""

    name = "fake-sandbox"

    def __init__(self, rendered: str) -> None:
        self._rendered = rendered
        self.launched: list[dict] = []
        self.destroyed = 0

    async def launch(self, spec: dict):
        self.launched.append(spec)
        return {"id": "sbx-1", "spec": spec}

    async def exec(self, sandbox, call: ToolCall) -> dict:
        return {"status": 200, "html": self._rendered, "url": call.args["url"]}

    async def destroy(self, sandbox) -> None:
        self.destroyed += 1


def _shell_fetch() -> HttpFetch:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_SHELL)

    return HttpFetch(allow_private=True, transport=httpx.MockTransport(handler))


def _registry_with_tiers(backend: _FakeBackend):
    from zu_core.registry import Registry
    from zu_detectors.js_shell import JsShellDetector
    from zu_tools.render import RenderDom

    reg = Registry()
    reg.register("tools", "http_fetch", _shell_fetch())          # tier 1
    reg.register("tools", "render_dom", RenderDom(backend=backend))  # tier 2
    reg.register("detectors", "js-shell", JsShellDetector())
    return reg


async def test_escalation_climbs_to_tier2_and_succeeds() -> None:
    # The step-5 story: tier-1 fetch returns a JS shell -> js-shell detector
    # escalates -> the loop climbs to tier 2 -> render_dom (a browser) returns
    # the real page -> the model finalises. The same job, one tier higher.
    backend = _FakeBackend(_RENDERED)
    provider = _RecordingProvider(
        [
            {"tool": "http_fetch", "args": {"url": "http://spa.test/"}},   # tier 1: shell
            {"tool": "render_dom", "args": {"url": "http://spa.test/"}},   # tier 2: real DOM
            {"text": '{"title": "Acme Widget", "price": "$9.00"}', "finish": "stop"},
        ]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="get title and price"), provider, _registry_with_tiers(backend), bus)

    assert result.status == Status.SUCCESS
    assert result.value == {"title": "Acme Widget", "price": "$9.00"}

    # The browser tier was actually leased and torn down.
    assert backend.launched and backend.destroyed == 1
    assert backend.launched[0]["tier"] == 2

    # render_dom was withheld at tier 1 and only offered after the climb.
    assert "render_dom" not in provider.tools_seen[0]
    assert "render_dom" in provider.tools_seen[-1]

    # The escalation is recorded as a tier climb, not a terminal escalation.
    escalated = [e for e in await bus.query() if e.type == "harness.task.escalated"]
    assert len(escalated) == 1
    assert escalated[0].payload == {
        "reason": "js-shell",
        "detail": escalated[0].payload["detail"],
        "from_tier": 1,
        "to_tier": 2,
    }
    assert "exhausted" not in escalated[0].payload
    # The detector fired and the run completed at the higher tier.
    types = [e.type for e in await bus.query()]
    assert "harness.detector.fired" in types
    assert types[-1] == "harness.task.completed"


async def test_escalation_via_real_builtin_detectors() -> None:
    # End-to-end through the *real* built-in detectors (empty/error/js-shell/
    # bot-wall registered together), not a hand-rolled one: a JS-shell page must
    # drive the climb via js-shell's own logic, with the others coexisting.
    from zu_detectors.bot_wall import BotWallDetector
    from zu_detectors.empty import EmptyDetector
    from zu_detectors.error import ErrorDetector
    from zu_detectors.js_shell import JsShellDetector
    from zu_tools.render import RenderDom

    backend = _FakeBackend(_RENDERED)
    reg = Registry()
    reg.register("tools", "http_fetch", _shell_fetch())
    reg.register("tools", "render_dom", RenderDom(backend=backend))
    for name, det in [
        ("empty", EmptyDetector()),
        ("error", ErrorDetector()),
        ("js-shell", JsShellDetector()),
        ("bot-wall", BotWallDetector()),
    ]:
        reg.register("detectors", name, det)

    provider = ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": "http://spa.test/"}},
            {"tool": "render_dom", "args": {"url": "http://spa.test/"}},
            {"text": '{"title": "Acme Widget", "price": "$9.00"}', "finish": "stop"},
        ]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="x"), provider, reg, bus)

    assert result.status == Status.SUCCESS
    assert backend.launched and backend.destroyed == 1
    escalated = [e for e in await bus.query() if e.type == "harness.task.escalated"]
    assert len(escalated) == 1 and escalated[0].payload["reason"] == "js-shell"


async def test_turn_usage_is_recorded_for_cost() -> None:
    # Cost is reconstructable from the log alone: each model call emits a
    # harness.turn.completed carrying its tier and the provider's usage dict.
    moves = [
        ModelResponse(
            tool_calls=[ToolCall(name="http_fetch", args={"url": "http://example.test/"})],
            finish=Finish.TOOL_CALLS,
            usage={"input_tokens": 100, "output_tokens": 20},
        ),
        ModelResponse(text='{"ok": true}', finish=Finish.STOP, usage={"input_tokens": 50, "output_tokens": 10}),
    ]
    bus = EventBus()
    await run_task(TaskSpec(query="x"), ScriptedProvider(moves), _registry_with_tools(), bus)

    turns = [e for e in await bus.query() if e.type == "harness.turn.completed"]
    assert len(turns) == 2
    assert turns[0].payload["tier"] == 1
    assert turns[0].payload["usage"] == {"input_tokens": 100, "output_tokens": 20}
    # the basis of cost: total tokens, summed straight from the log
    total = sum(
        t.payload["usage"].get("input_tokens", 0) + t.payload["usage"].get("output_tokens", 0)
        for t in turns
    )
    assert total == 180


async def test_turn_completed_attributes_tokens_to_the_right_tier() -> None:
    # After a climb, usage is attributed to the tier that produced it — the
    # per-tier breakdown a savings (cheap-tier-first) calculation needs.
    backend = _FakeBackend(_RENDERED)
    provider = ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": "http://spa.test/"}},  # tier 1
            {"tool": "render_dom", "args": {"url": "http://spa.test/"}},  # tier 2 (after climb)
            {"text": '{"x": 1}', "finish": "stop"},
        ]
    )
    bus = EventBus()
    await run_task(TaskSpec(query="x"), provider, _registry_with_tiers(backend), bus)
    tiers = [e.payload["tier"] for e in await bus.query() if e.type == "harness.turn.completed"]
    assert tiers == [1, 2, 2]  # turn 1 at tier 1; climbed, so the rest at tier 2


async def test_tier2_tool_is_withheld_before_escalation() -> None:
    # The ladder is enforced on dispatch, not just on what the model is shown:
    # calling a tier-2 tool before any escalation hits the unknown-tool branch.
    backend = _FakeBackend(_RENDERED)
    provider = ScriptedProvider.from_moves(
        [
            {"tool": "render_dom", "args": {"url": "http://spa.test/"}},  # not yet unlocked
            {"text": "{}", "finish": "stop"},
        ]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="x"), provider, _registry_with_tiers(backend), bus)

    assert result.status == Status.SUCCESS  # the loop didn't crash
    assert backend.launched == []  # the browser was never leased
    returned = next(e for e in await bus.query() if e.type == "harness.tool.returned")
    assert "unknown tool" in returned.payload["observation"]["error"]


async def test_checkpoint_acts_on_worst_verdict_not_first() -> None:
    # Regression: a checkpoint must act on the WORST verdict, not the first one
    # in registry order. 'a-escalate' sorts before 'z-terminal', so iteration
    # sees ESCALATE first — but TERMINAL must still win, and both must be
    # recorded (every detector inspects the observation).
    class Esc:
        name = "a-escalate"
        scope = Scope.PER_OBSERVATION

        def inspect(self, ctx) -> Verdict:
            return Verdict(severity=Severity.ESCALATE, detector=self.name, detail="x")

    class Term:
        name = "z-terminal"
        scope = Scope.PER_OBSERVATION

        def inspect(self, ctx) -> Verdict:
            return Verdict(severity=Severity.TERMINAL, detector=self.name, detail="y")

    reg = _registry_with_tools()
    reg.register("detectors", "a-escalate", Esc())
    reg.register("detectors", "z-terminal", Term())
    provider = ScriptedProvider.from_moves(
        [{"tool": "http_fetch", "args": {"url": "http://example.test/"}}, {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="x"), provider, reg, bus)
    assert result.status == Status.TERMINAL
    assert result.reason == "z-terminal"
    fired = {e.payload["detector"] for e in await bus.query() if e.type == "harness.detector.fired"}
    assert fired == {"a-escalate", "z-terminal"}  # both recorded, worst acted on


async def test_404_empty_page_terminates_not_escalates() -> None:
    # The real-built-in trigger for the same bug: a 404 with an empty body fires
    # both `error` (TERMINAL) and `empty` (ESCALATE, sorts first). It must
    # terminate on the 404, never waste a tier climb on a dead page.
    from zu_detectors.empty import EmptyDetector
    from zu_detectors.error import ErrorDetector

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="")

    reg = Registry()
    reg.register(
        "tools", "http_fetch", HttpFetch(allow_private=True, transport=httpx.MockTransport(handler))
    )
    reg.register("detectors", "empty", EmptyDetector())
    reg.register("detectors", "error", ErrorDetector())
    provider = ScriptedProvider.from_moves(
        [{"tool": "http_fetch", "args": {"url": "http://x.test/"}}, {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="x"), provider, reg, bus)
    assert result.status == Status.TERMINAL
    assert result.reason == "error"
    assert not [e for e in await bus.query() if e.type == "harness.task.escalated"]


async def test_escalation_exhausted_when_no_higher_tier() -> None:
    # With max_tier pinned to 1, an escalating detector has nowhere to climb,
    # so the run ends with an ESCALATE Result naming the detector (and the
    # event records the exhaustion rather than a climb).
    backend = _FakeBackend(_RENDERED)
    provider = ScriptedProvider.from_moves(
        [{"tool": "http_fetch", "args": {"url": "http://spa.test/"}}, {"text": "{}", "finish": "stop"}]
    )
    bus = EventBus()
    result = await run_task(
        TaskSpec(query="x", max_tier=1), provider, _registry_with_tiers(backend), bus
    )
    assert result.status == Status.ESCALATE
    assert result.reason == "js-shell"
    escalated = next(e for e in await bus.query() if e.type == "harness.task.escalated")
    assert escalated.payload["exhausted"] is True
    assert backend.launched == []  # never climbed, so tier 2 was never leased
