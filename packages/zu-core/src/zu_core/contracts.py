"""The typed boundaries everything in Zu speaks through.

These three frozen/validated Pydantic models — TaskSpec, Result, Event — are
the gates every part of the runtime passes through. They are deliberately
strict: a malformed task or a mis-namespaced event must be refused at the
boundary, not swallowed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, field_validator


class Status(str, Enum):
    SUCCESS = "success"
    ESCALATE = "escalate"
    TERMINAL = "terminal"
    # The run suspended at a human-in-the-loop ESCALATE (ZU-CD-1/2/5): a gate or
    # detector requested approval of a specific invocation. The run is NOT
    # finished — its resumable state is on the log (``harness.run.paused``), and
    # ``run_task(resume_from=...)`` continues it once a human resolution lands.
    PAUSED = "paused"


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
    # Run-level taint (ZU-CD-3): set when this run ingests untrusted/hostile
    # input (e.g. a caller folding a ``TriggerEvent`` whose ``hostile`` flag is
    # set into the query). The loop records it on ``harness.task.started`` and
    # exposes it to gates/validators via ``RunContext.tainted`` so a gate can
    # force-escalate a high-consequence action once the run is tainted. It is a
    # coarse, mechanical, run-level flag — never a policy self-report.
    tainted: bool = False


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

    Two fields carry the tamper-evidence chain (ZU-AUDIT-1): ``prev_hash`` and
    ``hash``. They default to ``None`` and are **set by the canonical sink at
    append time** (see ``zu_core.chain``), not by an emitter — the sink is the
    single ordering authority, so the chain is computed where order is decided.
    A run/replay verifies integrity with ``chain.verify_chain``; a break means
    an event was reordered, inserted, deleted, or its content edited.

    Consumer-defined fields convention (ZU-AUDIT-3): a consumer layering its own
    chain on Zu (e.g. ``grant_id``, ``consent_ref``, ``capability_id``, the
    verified peer identity, ``idempotency_key``) puts them under a reserved
    ``payload["ctx"]`` sub-dict, so they never collide with harness payload keys
    and a sink can index the subset the consumer registers
    (``eventstore.register_event_filter``).
    """

    model_config = {"frozen": True}

    event_id: UUID = Field(default_factory=uuid4)
    trace_id: UUID
    task_id: UUID
    parent_id: UUID | None = None
    type: str
    ts: datetime = Field(default_factory=lambda: datetime.now(UTC))
    source: str
    payload: dict = Field(default_factory=dict)
    schema_version: int = 1
    # Tamper-evidence chain (ZU-AUDIT-1), set by the canonical sink at append:
    # ``hash`` is sha256 over this event's canonical content + ``prev_hash``;
    # ``prev_hash`` is the predecessor's ``hash`` in this event's trace chain.
    prev_hash: str | None = None
    hash: str | None = None

    @field_validator("type")
    @classmethod
    def _namespace(cls, v: str) -> str:
        if not (v.startswith("harness.") or v.startswith("data.")):
            raise ValueError("event type must start with 'harness.' or 'data.'")
        return v
