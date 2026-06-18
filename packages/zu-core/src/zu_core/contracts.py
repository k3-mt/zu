"""The typed boundaries everything in Zu speaks through.

These three frozen/validated Pydantic models — TaskSpec, Result, Event — are
the gates every part of the runtime passes through. They are deliberately
strict: a malformed task or a mis-namespaced event must be refused at the
boundary, not swallowed.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class Status(str, Enum):
    SUCCESS = "success"
    ESCALATE = "escalate"
    TERMINAL = "terminal"


class Budget(BaseModel):
    max_steps: int = 20
    max_tokens: int = 200_000
    wall_time_s: int = 120
    max_tool_calls: int = 32  # per single model response — caps a runaway turn


class TaskSpec(BaseModel):
    """The typed input to a run."""

    task_id: UUID = Field(default_factory=uuid4)
    query: str
    target: str | None = None
    output_schema: dict = Field(default_factory=dict)  # JSON schema the result must satisfy
    budget: Budget = Field(default_factory=Budget)
    max_tier: int = 2


class Result(BaseModel):
    """The typed output of a run."""

    status: Status
    value: dict | None = None
    reason: str | None = None  # detector name, on escalate/terminal


class Event(BaseModel):
    """The append-only record envelope.

    Frozen at the envelope level: no field may be *reassigned* once an event is
    built. The durable record is immutable in the strongest sense — a sink
    serialises the event to JSON at ``append`` time, so what lands in the
    canonical store can never change afterward.

    One boundary to know: ``frozen`` does not deep-freeze the ``payload`` dict's
    *contents* (``event.payload[k] = ...`` is not blocked). Deep-freezing every
    payload was rejected deliberately — payloads carry large fetched HTML on the
    hot path and copying/freezing them per event is too costly. The invariant is
    therefore: **treat a published event's payload as read-only.** Do not mutate
    it in place; the canonical on-disk copy is already immutable regardless.
    """

    model_config = {"frozen": True}

    event_id: UUID = Field(default_factory=uuid4)
    trace_id: UUID
    task_id: UUID
    parent_id: UUID | None = None
    type: str
    ts: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    source: str
    payload: dict = Field(default_factory=dict)
    schema_version: int = 1

    @field_validator("type")
    @classmethod
    def _namespace(cls, v: str) -> str:
        if not (v.startswith("harness.") or v.startswith("data.")):
            raise ValueError("event type must start with 'harness.' or 'data.'")
        return v
