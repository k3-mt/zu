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
# Emitted once at run start with the capability envelope every active tool
# declared: {"tools": {name: {tier, capabilities, egress}}}. This is the
# machine-readable record the gate's out-of-band verdict observers compare
# observed behaviour against — "did a plugin reach a host outside its declared
# egress?" is answerable only because the declaration is on the log.
ENVELOPE_DECLARED = "harness.envelope.declared"
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
# A contained adversarial/unsafe attempt: a guard refused an action (SSRF/egress
# block, an oversized "schema bomb" observation, a denied capability). Emitted at
# the point of containment so a blocked attempt is on the record, never silent —
# the raw material for the defense review queue and the live dashboard.
DEFENSE_BLOCKED = "harness.defense.blocked"

# Out-of-band facts emitted by the red-team control plane, NOT by a tool
# (RED_TEAM_CONTAINER.md §3.3). They are produced OUTSIDE the target container —
# by the egress proxy and the host-effect monitor — so a deceptive plugin can
# neither author nor forge them. The verdict observers prefer them when present.
#
#   egress.observed     -> the proxy's authoritative record of a connection the
#                          target actually made: {client, host, ip, port, scheme,
#                          bytes_out, allowed}. Unlike data.source.fetched (the
#                          tool's SELF-REPORT of where it went), this is ground
#                          truth — what closes the "reaches evil, reports allowed"
#                          gap the in-process gate cannot (RED_TEAM.md §1).
#   host_effect.observed-> an observed filesystem write / subprocess spawn from
#                          outside the target's userland: {kind, path|argv, pid}.
#                          Lets HostEffect fire on an OBSERVED, undeclared effect,
#                          not only a declared capability.
EGRESS_OBSERVED = "harness.egress.observed"
HOST_EFFECT_OBSERVED = "harness.host_effect.observed"

# --- the security gate, taint, durable state, and human escalation -----------
# The pre-execution gate's auditable decision (ZU-CORE-2 / ZU-AUDIT-2): emitted
# for every non-None InvocationGate verdict, BEFORE the tool runs. Payload:
# {"action_ref": <event_id of harness.tool.invoked>, "decision":
# "allow"|"escalate"|"deny"|"approved_by_human", "gate", "rule_id", "detail",
# "escalation_id"?}. ``action_ref`` (and parent_id) join action -> the rule that
# decided it, so replay reconstructs "which rule allowed/blocked this call".
GATE_DECIDED = "harness.gate.decided"
# Run-level taint flipped false->true mid-run (ZU-CD-3): {"source", "detail"}.
# Run-start taint rides on harness.task.started's payload ("tainted": bool); this
# records a later flip so rebuild-from-log reconstructs the flag on resume.
TAINT_RAISED = "harness.taint.raised"
# A durable per-grant state write (ZU-CD-4): {"grant_id", "key", "value"}. The
# in-memory GrantStore is a cache over these events; resume folds them to rebuild
# cumulative counters (velocity / spend-so-far) so limits survive pause/resume.
GRANT_UPDATED = "harness.grant.updated"
# Human-in-the-loop ESCALATE (ZU-CD-1/2). ``approval.requested`` carries the
# LITERAL harness-held invocation as ground truth (NOT model narration):
# {"approval_id", "tool", "args", "idempotency_key", "reason", "detail"}.
# ``approval.resolved`` is the event-sourced human decision, bound to the exact
# invocation by approval_id AND idempotency_key: {"approval_id", "decision":
# "approve"|"deny", "by", "idempotency_key", "binding_ref"?}.
APPROVAL_REQUESTED = "harness.approval.requested"
APPROVAL_RESOLVED = "harness.approval.resolved"
# Pause/resume snapshot + marker (ZU-CD-5). ``run.paused`` carries the resumable
# state rebuilt on resume: {"approval_id", "tier", "tokens", "tainted", "step",
# "pending": {"tool", "args", "idempotency_key"}}. ``run.resumed`` marks a
# resumed run re-entering the loop from a prior log.
RUN_PAUSED = "harness.run.paused"
RUN_RESUMED = "harness.run.resumed"
# A replayed track was verified against a human-approved content hash (ZU-RAIL-1):
# {"rail_hash"}. The run refuses to replay an unapproved rail (a content-hash
# mismatch is recorded as harness.defense.blocked {kind:"rail_unapproved"}).
RAIL_VERIFIED = "harness.rail.verified"
# A capability-bearing tool call was DISARMED in explore mode (ZU-RAIL-2):
# {"tool"}. The tool did not execute — a stub observation was returned instead, so
# pathfinding on a hostile surface is never armed with live instruments.
RAIL_DISARMED = "harness.rail.disarmed"

# --- harness.pipeline.* — multi-phase orchestration (zu.Pipeline) ------------
# A pipeline chains runs under ONE shared trace_id; these record its boundaries
# so the whole multi-phase run is itself lossless and replayable. ``phase.skipped``
# is the resume signal — a phase already complete on the log is not re-executed.
PIPELINE_STARTED = "harness.pipeline.started"
PIPELINE_PHASE_STARTED = "harness.pipeline.phase.started"
PIPELINE_PHASE_COMPLETED = "harness.pipeline.phase.completed"
PIPELINE_PHASE_SKIPPED = "harness.pipeline.phase.skipped"
PIPELINE_COMPLETED = "harness.pipeline.completed"
PIPELINE_FAILED = "harness.pipeline.failed"

# --- data.* — what the agent read and produced -------------------------------
SOURCE_FETCHED = "data.source.fetched"
RECORD_EXTRACTED = "data.record.extracted"

HARNESS_TYPES: frozenset[str] = frozenset(
    {
        TASK_STARTED,
        ENVELOPE_DECLARED,
        TASK_COMPLETED,
        TASK_ESCALATED,
        TASK_TERMINAL,
        TURN_STARTED,
        TURN_COMPLETED,
        TOOL_INVOKED,
        TOOL_RETURNED,
        DETECTOR_FIRED,
        VALIDATION_FAILED,
        DEFENSE_BLOCKED,
        EGRESS_OBSERVED,
        HOST_EFFECT_OBSERVED,
        GATE_DECIDED,
        TAINT_RAISED,
        GRANT_UPDATED,
        APPROVAL_REQUESTED,
        APPROVAL_RESOLVED,
        RUN_PAUSED,
        RUN_RESUMED,
        RAIL_VERIFIED,
        RAIL_DISARMED,
        PIPELINE_STARTED,
        PIPELINE_PHASE_STARTED,
        PIPELINE_PHASE_COMPLETED,
        PIPELINE_PHASE_SKIPPED,
        PIPELINE_COMPLETED,
        PIPELINE_FAILED,
    }
)
DATA_TYPES: frozenset[str] = frozenset({SOURCE_FETCHED, RECORD_EXTRACTED})
ALL_TYPES: frozenset[str] = HARNESS_TYPES | DATA_TYPES
