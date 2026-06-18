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
# Emitted in two shapes, distinguished by payload (the documented contract):
#   climb  -> {"reason", "detail", "from_tier", "to_tier"}  the escalation step
#   exhaust-> {"reason", "tier", "exhausted": true}         no higher tier; run ends
# A consumer keys on ``to_tier`` (progress) vs ``exhausted`` (terminal ESCALATE).
TASK_ESCALATED = "harness.task.escalated"
TASK_TERMINAL = "harness.task.terminal"
TURN_STARTED = "harness.turn.started"
# Emitted once per model call with {step, tier, model, usage} — the per-turn
# token usage and the tier/model that produced it. This is the raw material a
# cost/savings projection sums over the log (the log is the source of truth;
# the aggregate is a read-side view). ``usage`` is the provider's usage dict
# (e.g. input_tokens/output_tokens), empty when the provider reports none.
TURN_COMPLETED = "harness.turn.completed"
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
        TURN_COMPLETED,
        TOOL_INVOKED,
        TOOL_RETURNED,
        DETECTOR_FIRED,
        VALIDATION_FAILED,
    }
)
DATA_TYPES: frozenset[str] = frozenset({SOURCE_FETCHED, RECORD_EXTRACTED})
ALL_TYPES: frozenset[str] = HARNESS_TYPES | DATA_TYPES
