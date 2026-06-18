"""Build step 4 — the interpreter loop + tier-1 tools + budgets.

Proves the loop drives provider -> tool -> observation -> finalise with the
ScriptedProvider (fake model) and a fixtured page, **deterministically**: the
same Result and the same sequence of event types every run, with no network.
Budgets, tool-error isolation, and the detector checkpoint are exercised too.
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
        "harness.tool.invoked",
        "data.source.fetched",  # the fetch carried html -> a data event
        "harness.tool.returned",
        "harness.turn.started",
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

    async def complete(self, req):
        self.seen.append([dict(m) for m in req.messages])
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
