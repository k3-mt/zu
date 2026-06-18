"""The MLR event taxonomy — the small, stable set of event types.

These are the canonical names the harness emits. Keeping them as constants
(rather than stringly-typed literals scattered across emitters) means the loop,
projections, and detectors all agree on one spelling, and the set is the
documented contract that OTel / central-log shippers will later map from.

Every name is namespaced ``harness.*`` or ``data.*`` — the same rule the
``Event.type`` validator enforces in contracts.py.
"""

from __future__ import annotations

# --- harness.* — the runtime's own lifecycle ---------------------------------
TASK_STARTED = "harness.task.started"
TASK_COMPLETED = "harness.task.completed"
TASK_ESCALATED = "harness.task.escalated"
TASK_TERMINAL = "harness.task.terminal"
TURN_STARTED = "harness.turn.started"
TOOL_INVOKED = "harness.tool.invoked"
TOOL_RETURNED = "harness.tool.returned"
DETECTOR_FIRED = "harness.detector.fired"
VALIDATION_FAILED = "harness.validation.failed"

# --- data.* — what the agent read and produced -------------------------------
SOURCE_FETCHED = "data.source.fetched"
RECORD_EXTRACTED = "data.record.extracted"

HARNESS_TYPES: frozenset[str] = frozenset(
    {
        TASK_STARTED,
        TASK_COMPLETED,
        TASK_ESCALATED,
        TASK_TERMINAL,
        TURN_STARTED,
        TOOL_INVOKED,
        TOOL_RETURNED,
        DETECTOR_FIRED,
        VALIDATION_FAILED,
    }
)
DATA_TYPES: frozenset[str] = frozenset({SOURCE_FETCHED, RECORD_EXTRACTED})
ALL_TYPES: frozenset[str] = HARNESS_TYPES | DATA_TYPES
