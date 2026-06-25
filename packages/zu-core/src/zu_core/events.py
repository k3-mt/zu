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
# A Monitor (the stateful, history-aware generalisation of a Detector, ZU-RAIL-5)
# emitted a non-OK verdict over the event stream. Payload:
# {"monitor": str, "state": "warn"|"violation", "detail": str|None, "step": int|None}.
# Parented to the turn, ``source`` is the monitor name. Mirrors harness.detector.fired;
# a "violation" maps to a TERMINAL Verdict through the loop's existing halting path.
MONITOR_FIRED = "harness.monitor.fired"
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
# A consumer marked a last-known-good (LKG) rollback point (ZU-RAIL-8). Payload:
# {"label": str, "step": int}. Parented to run.root. ``last_known_good`` returns
# the most recent such marker's event_id as the restore target.
CHECKPOINT_MARKED = "harness.checkpoint.marked"
# The run was re-seated at a prior LKG event for an on-rail re-plan (ZU-RAIL-8) —
# distinct from harness.run.resumed, which moves FORWARD past a pause. Payload:
# {"to": str(event_id of the LKG event), "dropped": int (count of events after the
# LKG that were truncated)}. Parented to run.root. The good prefix is folded; the
# failed tail is dropped; consume-once claims from the good prefix are preserved.
RUN_ROLLED_BACK = "harness.run.rolled_back"
# A consume-once execution claim (ZU-CD-6): {"key"}. The first claim of a key wins
# and is recorded here; the in-memory ExecutionLedger is a cache over these events,
# so a resumed/replayed run folds them to rebuild the claimed set and REFUSES to
# execute an already-claimed side effect again (one approval -> one irreversible act).
EXECUTION_CLAIMED = "harness.execution.claimed"
# --- the credential broker — scoped/audited USE of an instrument (§8) ---------
# A capability was USED against an instrument (ZU-AUDIT-5): the OUTCOME summary on
# the record, NEVER the secret. Payload:
# {"operation", "outcome": {charge_id|token-ref summary}, "ctx": {"grant_id",
#  "consent_id", "capability_id", "instrument_ref", "idempotency_key"}}. The
# consumer-field convention (payload["ctx"], ZU-AUDIT-3) binds the use to the
# authorizing consent so "acted-within-granted-authority" is provable from the
# chain. Emitted by a CredentialBroker only on a FULL allow (the instrument ran).
CAPABILITY_USED = "harness.capability.used"
# A grant (capability) was issued/registered with a broker (§8): {"ctx":
# {"grant_id", "instrument_ref", "consent_id"}, "scope": <summary>}. The opaque
# handle is recorded; the secret is not (it lives behind the Instrument). A refused
# use reuses harness.defense.blocked; a cumulative-cap write reuses harness.grant.updated.
GRANT_ISSUED = "harness.grant.issued"
# A grant was revoked (§8): {"ctx": {"grant_id"}}. Every subsequent use of the
# handle is refused (and logged as harness.defense.blocked {kind:"capability_revoked"}).
GRANT_REVOKED = "harness.grant.revoked"
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
# The action surface shown to the policy at one step (Engineering Design §4.5 /
# §11). data.* because it is perception the agent CONSUMED — the reviewer's record
# of "what could the agent perceive/do here". Payload:
# {"url": str, "title": str, "affordances": int (count), "handles": list[str],
#  "context": int (count), "blind": bool, "blind_reason": str|None}. The durable
# role+name locators stay harness-side (the handle_map is NOT on the log); the
# handle list + counts are the auditable surface. Emitted by ActionSurface when
# op=open/reduce yields a surface.
SURFACE_CAPTURED = "data.surface.captured"
# One pointer move/click trajectory the agent PRODUCED (Engineering Design §5.4 /
# §12). data.* because it is an agent-produced action on the world — the audit
# answer to "where did the cursor go". The full per-sample path rides in the tool
# observation for replay; the event keeps the cheap summary. Payload:
# {"handle": str|None, "clicked": bool, "samples": int (count), "duration_ms":
#  float, "dest": {"x": float, "y": float}, "seed": str}. Emitted by PointerControl
# after a successful dispatch.
POINTER_DISPATCHED = "data.pointer.dispatched"
# A pattern recognizer matched an archetype over one step's action surface
# (Engineering Design §5). data.* because it is perception the agent
# INFERRED — the auditable record of "what did the agent recognize here" — NOT
# an instruction it obeyed: a recognized pattern is a PRIOR, verified by a rail
# Monitor (ZU-RAIL-9), never ground truth. A low-confidence recognition emits
# NO event (no hint masquerading as fact). Payload:
# {"archetype": str, "confidence": float, "matched_handles": list[str],
#  "blind": bool}. Parented to the turn.
PATTERN_RECOGNIZED = "data.pattern.recognized"

# --- data.shadow.* — author-by-demonstration capture (§2.8) ------------------
# A Shadow recording IS the event bus run over a HUMAN session — the human is the
# policy for that one run, so these are ``data.*`` (perception/action the session
# produced), recorded by zu-shadow's recorder over an abstract input/CDP stream.
# REDACTION runs BEFORE any of these reach EventSink.append (ZU-AUDIT-4): secrets
# never touch the log. CAPTURE IS SEMANTIC — a user action is named by its target's
# {role, name, label} (reusing zu_core.surface), NEVER a CSS selector or pixel
# coordinate (the shared currency with §4 handles / §5 SurfaceView).
#
#   session.start    -> {"site": str, "started_by": "human"}. The recording's root.
#   session.end      -> {"outcome": str|None, "steps": int (count)}. The terminal.
#   user.click       -> {"target": {"role", "name", "label"}, "intent": str|None}.
#                       ``intent`` is the OPTIONAL reviewed "why" affordance — the
#                       human's narration of the step, REDACTED like everything else,
#                       NEVER auto-promoted into the synthesized agent.
#   user.type        -> {"target": {"role","name","label"}, "value": str (REDACTED —
#                        a password/token never reaches here), "intent": str|None}.
#   user.navigate    -> {"url": str (REDACTED of credentials/tokens), "intent": str|None}.
#   page.loaded      -> {"url": str, "title": str}. A page settled — the locus a
#                       subsequent action's semantic target resolves against.
#   network.response -> {"url": str, "status": int, "host": str}. The metadata a
#                       response carried — bodies/headers are NOT recorded (and any
#                       auth header is redacted at source). The synthesized agent's
#                       EGRESS ALLOWLIST WRITES ITSELF from the ``host`` values here.
SHADOW_SESSION_START = "data.shadow.session.start"
SHADOW_SESSION_END = "data.shadow.session.end"
SHADOW_USER_CLICK = "data.shadow.user.click"
SHADOW_USER_TYPE = "data.shadow.user.type"
SHADOW_USER_NAVIGATE = "data.shadow.user.navigate"
# A settled scroll. Payload: {"direction": "up"|"down", "y": int}. Context (not an action
# step) — it records that the human had to scroll to reach the next affordance.
SHADOW_USER_SCROLL = "data.shadow.user.scroll"
SHADOW_PAGE_LOADED = "data.shadow.page.loaded"
SHADOW_NETWORK_RESPONSE = "data.shadow.network.response"

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
        MONITOR_FIRED,
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
        CHECKPOINT_MARKED,
        RUN_ROLLED_BACK,
        EXECUTION_CLAIMED,
        CAPABILITY_USED,
        GRANT_ISSUED,
        GRANT_REVOKED,
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
DATA_TYPES: frozenset[str] = frozenset(
    {
        SOURCE_FETCHED,
        RECORD_EXTRACTED,
        SURFACE_CAPTURED,
        POINTER_DISPATCHED,
        PATTERN_RECOGNIZED,
        SHADOW_SESSION_START,
        SHADOW_SESSION_END,
        SHADOW_USER_CLICK,
        SHADOW_USER_TYPE,
        SHADOW_USER_NAVIGATE,
        SHADOW_USER_SCROLL,
        SHADOW_PAGE_LOADED,
        SHADOW_NETWORK_RESPONSE,
    }
)
ALL_TYPES: frozenset[str] = HARNESS_TYPES | DATA_TYPES
