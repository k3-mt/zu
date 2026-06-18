"""Smoke tests for the built-in validators and their discovery.

Wired against the full event log in build step 6; these lock the core
behavior — schema enforcement and the anti-hallucination grounding check — and
the entry-point contract now.
"""

from __future__ import annotations

from zu_core.contracts import Result, Status, TaskSpec
from zu_core.ports import RunContext, Severity
from zu_core.registry import Registry
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
    assert v is not None and v.detector == "schema"


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


def test_validators_discoverable() -> None:
    reg = Registry()
    reg.discover()
    for name in ("schema", "grounding"):
        assert name in reg.names("validators")
