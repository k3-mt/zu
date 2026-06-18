"""Build step 1 — Contracts.

Proves the runtime won't swallow a malformed task or hand back a malformed
event: good data is accepted; broken data — wrong type, or an event name that
isn't 'harness.*' or 'data.*' — is refused.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import ValidationError

from zu_core.contracts import Budget, Event, Result, Status, TaskSpec


def test_good_taskspec_accepted() -> None:
    spec = TaskSpec(query="extract the price", target="https://example.com")
    assert spec.query == "extract the price"
    assert spec.max_tier == 2
    assert spec.budget == Budget()  # defaults applied


def test_taskspec_rejects_wrong_type() -> None:
    with pytest.raises(ValidationError):
        TaskSpec(query=123)  # type: ignore[arg-type]


def test_good_event_accepted() -> None:
    ev = Event(
        trace_id=uuid4(),
        task_id=uuid4(),
        type="harness.task.started",
        source="loop",
    )
    assert ev.type == "harness.task.started"
    assert ev.schema_version == 1


def test_data_namespace_accepted() -> None:
    ev = Event(
        trace_id=uuid4(),
        task_id=uuid4(),
        type="data.record.extracted",
        source="loop",
    )
    assert ev.type.startswith("data.")


def test_event_rejects_bad_namespace() -> None:
    with pytest.raises(ValidationError):
        Event(
            trace_id=uuid4(),
            task_id=uuid4(),
            type="random.thing",  # not harness.* or data.*
            source="loop",
        )


def test_event_is_frozen() -> None:
    ev = Event(trace_id=uuid4(), task_id=uuid4(), type="harness.turn.started", source="loop")
    with pytest.raises(ValidationError):
        ev.type = "harness.task.completed"


def test_result_carries_status_and_reason() -> None:
    r = Result(status=Status.ESCALATE, reason="js-shell")
    assert r.status is Status.ESCALATE
    assert r.value is None
    assert r.reason == "js-shell"
