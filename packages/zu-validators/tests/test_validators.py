"""Tests for the built-in validators, their discovery, and their behaviour
inside the loop (build step 6).

These lock the core behaviour — schema enforcement and the anti-hallucination
grounding check, including token-boundary precision — and prove grounding works
against the real event log when run inside the interpreter loop (at finalise the
observation is gone, so grounding must read the data.source.fetched events).
"""

from __future__ import annotations

import httpx

from zu_core.bus import EventBus
from zu_core.contracts import Result, Status, TaskSpec
from zu_core.loop import run_task
from zu_core.ports import RunContext, Severity
from zu_core.registry import Registry
from zu_providers.scripted import ScriptedProvider
from zu_tools.fetch import HttpFetch
from zu_validators.grounding import GroundingValidator
from zu_validators.schema import SchemaValidator

_SCHEMA = {
    "type": "object",
    "properties": {"price": {"type": "string"}},
    "required": ["price"],
}


def _ctx(observation: dict | None = None) -> RunContext:
    spec = TaskSpec(query="extract the price", output_schema=_SCHEMA)
    return RunContext(spec=spec, observation=observation)


def test_schema_passes_valid_result() -> None:
    r = Result(status=Status.SUCCESS, value={"price": "$9.00"})
    assert SchemaValidator().check(r, _ctx()) is None


def test_schema_fails_missing_required() -> None:
    r = Result(status=Status.SUCCESS, value={})
    v = SchemaValidator().check(r, _ctx())
    # A plain data mismatch must be RETRY (the model can correct it) — NOT
    # TERMINAL; the loop branches on this severity, so lock it, not just "fired".
    assert v is not None and v.detector == "schema"
    assert v.severity == Severity.RETRY


def test_grounding_fails_invented_value() -> None:
    r = Result(status=Status.SUCCESS, value={"price": "$1000.00"})
    ctx = _ctx({"html": "<span class='price'>$9.00</span>"})
    v = GroundingValidator().check(r, ctx)
    assert v is not None and "not found" in (v.detail or "")


def test_grounding_passes_value_on_page() -> None:
    r = Result(status=Status.SUCCESS, value={"price": "$9.00"})
    ctx = _ctx({"html": "<span class='price'>$9.00</span>"})
    assert GroundingValidator().check(r, ctx) is None


def test_grounding_checks_numeric_values() -> None:
    # A fabricated *number* must not pass ungrounded (the old code skipped
    # every non-string value, so invented prices/counts sailed through).
    invented = Result(status=Status.SUCCESS, value={"stock": 4096})
    ctx = _ctx({"html": "<span>in stock: 7</span>"})
    assert GroundingValidator().check(invented, ctx) is not None

    real = Result(status=Status.SUCCESS, value={"stock": 7})
    assert GroundingValidator().check(real, ctx) is None


def test_grounding_normalizes_whitespace() -> None:
    # Whitespace/case differences between the value and the page shouldn't fail.
    r = Result(status=Status.SUCCESS, value={"title": "Hello   World"})
    ctx = _ctx({"html": "<h1>hello world</h1>"})
    assert GroundingValidator().check(r, ctx) is None


def test_grounding_rejects_short_value_inside_larger_token() -> None:
    # Token-boundary precision: "5" must NOT be grounded by "1985" (plain
    # substring matching would have let the fabricated rating pass).
    only_in_year = _ctx({"html": "<p>The product launched in 1985.</p>"})
    assert GroundingValidator().check(Result(status=Status.SUCCESS, value={"rating": 5}), only_in_year) is not None

    # A genuinely standalone "5" on the page still grounds.
    standalone = _ctx({"html": "<p>Rated 5 stars by 1985 reviewers.</p>"})
    assert GroundingValidator().check(Result(status=Status.SUCCESS, value={"rating": 5}), standalone) is None


def test_grounding_rejects_value_inside_a_decimal() -> None:
    # A decimal point is a token boundary for *words*, but a fabricated number
    # must not be grounded by a fragment of a larger number: "14" is not on a
    # page that only says "$3.14", nor is "3".
    page = _ctx({"html": "<span class='price'>$3.14</span>"})
    assert GroundingValidator().check(Result(status=Status.SUCCESS, value={"n": 14}), page) is not None
    assert GroundingValidator().check(Result(status=Status.SUCCESS, value={"n": 3}), page) is not None
    # The whole decimal still grounds, and so does an integer the dot merely ends
    # a sentence after (the dot is not flanked by a digit on its outer side).
    assert GroundingValidator().check(Result(status=Status.SUCCESS, value={"p": "3.14"}), page) is None
    qty = _ctx({"html": "<p>Qty: 5.</p>"})
    assert GroundingValidator().check(Result(status=Status.SUCCESS, value={"q": 5}), qty) is None


def test_grounding_rejects_value_inside_a_compound_token() -> None:
    # A short number must not be grounded by a fragment of a date/version/time/
    # SKU/phone joined by - / : — "12" is not on a page that only says "12-2024".
    date = _ctx({"html": "<p>Released 12-2024 worldwide.</p>"})
    assert GroundingValidator().check(Result(status=Status.SUCCESS, value={"m": 12}), date) is not None
    ver = _ctx({"html": "<p>Build 4/19 shipped.</p>"})
    assert GroundingValidator().check(Result(status=Status.SUCCESS, value={"b": 19}), ver) is not None
    time = _ctx({"html": "<p>Starts at 12:30 sharp.</p>"})
    assert GroundingValidator().check(Result(status=Status.SUCCESS, value={"t": 30}), time) is not None
    # A genuinely standalone number flanked by a separator-with-no-adjacent-digit
    # (e.g. a slash ending a path segment) still grounds.
    path = _ctx({"html": "<a href='/items/42/'>item</a>"})
    assert GroundingValidator().check(Result(status=Status.SUCCESS, value={"id": 42}), path) is None


def test_grounding_is_unicode_token_aware() -> None:
    # The flank check is Unicode-aware (str.isalnum), so a value is not grounded
    # as a fragment of a non-ASCII word.
    page = _ctx({"html": "<p>café société</p>"})
    assert GroundingValidator().check(Result(status=Status.SUCCESS, value={"w": "caf"}), page) is not None
    assert GroundingValidator().check(Result(status=Status.SUCCESS, value={"w": "café"}), page) is None


def test_schema_error_is_terminal_not_a_crash() -> None:
    # An invalid output_schema (from the TaskSpec) raises jsonschema.SchemaError
    # internally; the validator must turn it into a TERMINAL verdict, never let
    # it escape and crash the validation ladder.
    spec = TaskSpec(query="x", output_schema={"type": "not-a-real-type"})
    ctx = RunContext(spec=spec, observation=None)
    r = Result(status=Status.SUCCESS, value={"a": 1})
    v = SchemaValidator().check(r, ctx)
    assert v is not None and v.severity == Severity.TERMINAL
    assert "invalid output_schema" in (v.detail or "")


def test_unresolvable_ref_is_terminal_not_a_crash() -> None:
    # A schema with an unresolvable $ref raises a *referencing* error that is NOT
    # a subclass of jsonschema.SchemaError — it would escape the old handlers and
    # crash the ladder. It must become a TERMINAL verdict like any other broken
    # schema, since the output_schema is untrusted TaskSpec input.
    for bad in ({"$ref": "#/nope"}, {"$ref": "http://evil.example/x"}):
        spec = TaskSpec(query="x", output_schema=bad)
        ctx = RunContext(spec=spec, observation=None)
        r = Result(status=Status.SUCCESS, value={"a": 1})
        v = SchemaValidator().check(r, ctx)
        assert v is not None and v.severity == Severity.TERMINAL
        assert "invalid output_schema" in (v.detail or "")


# --- grounding against the real event log, inside the loop -------------------

_PAGE = "<html><body><span class='price'>$9.00</span></body></html>"


def _loop_registry() -> Registry:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_PAGE)

    reg = Registry()
    reg.register(
        "tools", "http_fetch", HttpFetch(allow_private=True, transport=httpx.MockTransport(handler))
    )
    reg.register("validators", "schema", SchemaValidator())
    reg.register("validators", "grounding", GroundingValidator())
    return reg


async def test_grounding_in_loop_passes_value_from_event_log() -> None:
    # At finalise the loop passes no observation, so grounding must read the
    # price from the data.source.fetched event — the step-6 "against the event
    # log" promise, end to end.
    provider = ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": "http://x.test/"}},
            {"text": '{"price": "$9.00"}', "finish": "stop"},
        ]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="price", output_schema=_SCHEMA), provider, _loop_registry(), bus)
    assert result.status == Status.SUCCESS
    assert result.value == {"price": "$9.00"}
    types = [e.type for e in await bus.query()]
    assert "data.source.fetched" in types  # grounding had the log to read
    assert "harness.validation.failed" not in types


async def test_grounding_in_loop_rejects_fabrication_then_accepts_correction() -> None:
    # A price that is nowhere on the page fails grounding (RETRY); the loop feeds
    # the failure back and the corrected, grounded value then succeeds.
    provider = ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": "http://x.test/"}},
            {"text": '{"price": "$1000.00"}', "finish": "stop"},  # not on page -> RETRY
            {"text": '{"price": "$9.00"}', "finish": "stop"},  # grounded -> SUCCESS
        ]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="price", output_schema=_SCHEMA), provider, _loop_registry(), bus)
    assert result.status == Status.SUCCESS
    assert result.value == {"price": "$9.00"}
    failed = [e for e in await bus.query() if e.type == "harness.validation.failed"]
    assert failed and failed[0].payload["detector"] == "grounding"


async def test_grounding_corpus_ignores_the_models_own_text() -> None:
    # The model's output is recorded on harness.turn.completed (the live "train
    # of thought"). Grounding must NOT treat that as retrieved content, or a model
    # could ground a fabrication by simply emitting it. Only the fetched page
    # (which here does NOT contain the price) counts — so the value fails.
    provider = ScriptedProvider.from_moves(
        [
            {"tool": "http_fetch", "args": {"url": "http://x.test/"}},
            {"text": '{"price": "$1000.00"}', "finish": "stop"},  # spoken, not on the page
            {"text": '{"price": "$1000.00"}', "finish": "stop"},  # repeated — still ungrounded
        ]
    )
    bus = EventBus()
    result = await run_task(TaskSpec(query="price", output_schema=_SCHEMA), provider, _loop_registry(), bus)
    # Never succeeds: the price is only ever in the model's own text, never the page.
    assert result.status != Status.SUCCESS
    failed = [e for e in await bus.query() if e.type == "harness.validation.failed"]
    assert any(e.payload["detector"] == "grounding" for e in failed)


def test_validators_discoverable() -> None:
    reg = Registry()
    reg.discover()
    for name in ("schema", "grounding"):
        assert name in reg.names("validators")
