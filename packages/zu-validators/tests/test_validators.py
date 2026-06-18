"""Smoke tests for the built-in validators and their discovery.

Wired against the full event log in build step 6; these lock the core
behavior — schema enforcement and the anti-hallucination grounding check — and
the entry-point contract now.
"""

from __future__ import annotations

from zu_core.contracts import Result, Status, TaskSpec
from zu_core.ports import RunContext
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


def test_validators_discoverable() -> None:
    reg = Registry()
    reg.discover()
    for name in ("schema", "grounding"):
        assert name in reg.names("validators")
