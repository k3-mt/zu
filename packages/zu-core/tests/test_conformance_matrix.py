"""The conformance matrix guard.

Maps every ZU-* requirement to the concrete, offline proof that exercises it, and
asserts each proof exists — so a dropped or renamed proof is caught here rather
than silently leaving a requirement unverified. The human-readable matrix lives
in ``zu-upstream-conformance.md`` §9; this is its executable backstop.
"""

from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]

# requirement id -> (repo-relative path, symbol that must appear in it).
# A doc requirement (ZU-EXT-2) maps to its document with symbol None.
MATRIX: dict[str, tuple[str, str | None]] = {
    "ZU-CORE-1": ("packages/zu-core/tests/test_capability_acquisition.py", "test_call_to_ungranted_tool_reaches_nothing"),
    "ZU-CORE-2": ("packages/zu-core/tests/test_invocation_gate.py", "test_gate_deny_blocks_the_call_no_side_effect"),
    "ZU-CORE-3": ("packages/zu-backends/tests/test_oop_channel.py", "test_broker_secret_never_in_harness_memory"),
    "ZU-CORE-4": ("packages/zu-core/tests/test_invocation_gate.py", "test_idempotency_key_is_deterministic_across_replay"),
    # #48 — a tool must DECLARE its capability envelope; an undeclared off-box
    # tool must not slip past containment by reading as least privilege.
    "ZU-CORE-5": ("packages/zu-core/tests/test_envelope_declaration.py", "test_strict_mode_rejects_undeclared_envelope_and_admits_explicit_empty"),
    # #83 — a quarantined reader denies egress (empty effective tool set ⇒ declares
    # zero egress and never breaches a "required" posture) and isolates its state
    # (sharing a grant store / execution ledger fails loud).
    "ZU-CORE-6": ("packages/zu-core/tests/test_quarantine.py", "test_quarantine_denies_egress_and_isolates_state"),
    "ZU-NET-1": ("packages/zu-backends/tests/test_egress_enforce.py", "test_mechanism_is_swappable_without_core_change"),
    "ZU-NET-2": ("packages/zu-backends/tests/test_oop_channel.py", "test_channel_returns_derived_token_not_secret"),
    "ZU-NET-3": ("packages/zu-backends/tests/test_oop_channel.py", "test_broker_secret_never_in_harness_memory"),
    "ZU-NET-4": ("packages/zu-backends/tests/test_identity.py", "test_present_verify_roundtrip"),
    "ZU-NET-5": ("packages/zu-backends/tests/test_identity.py", "test_attestation_measurement_enforced_when_required"),
    "ZU-CD-1": ("packages/zu-core/tests/test_pause_resume.py", "test_pause_renders_ground_truth_then_resume_executes_once"),
    "ZU-CD-2": ("packages/zu-core/tests/test_pause_resume.py", "test_resume_with_wrong_key_is_rejected"),
    "ZU-CD-3": ("packages/zu-core/tests/test_invocation_gate.py", "test_taint_recorded_and_readable_at_the_gate"),
    "ZU-CD-4": ("packages/zu-core/tests/test_invocation_gate.py", "test_velocity_limit_via_grant_store"),
    "ZU-CD-5": ("packages/zu-core/tests/test_pause_resume.py", "test_resume_without_resolution_stays_paused"),
    "ZU-CD-6": ("packages/zu-core/tests/test_pause_resume.py", "test_resume_twice_executes_the_approved_side_effect_only_once"),
    # §8 — the credential broker: scoped/audited USE of an instrument WITHOUT the
    # policy ever holding the secret. ZU-CD-7/8 are the next integers in the FIXED
    # ZU-CD family; the proofs use a FAKE instrument + an adversarial ScriptedProvider.
    "ZU-CD-7": ("packages/zu-core/tests/test_credential_broker.py", "test_secret_never_reaches_the_policy_or_the_log"),
    "ZU-CD-8": ("packages/zu-core/tests/test_credential_broker.py", "test_over_authority_uses_are_refused_and_logged"),
    "ZU-AUDIT-1": ("packages/zu-core/tests/test_chain.py", "test_content_tamper_detected"),
    "ZU-AUDIT-2": ("packages/zu-core/tests/test_invocation_gate.py", "test_gate_deny_blocks_the_call_no_side_effect"),
    "ZU-AUDIT-3": ("packages/zu-core/tests/test_chain.py", "test_consumer_field_is_queryable"),
    # Capture-time redaction: secrets are stripped BEFORE any event reaches the
    # append-only log. The proof lives in zu-shadow/tests (repo-relative path resolves).
    "ZU-AUDIT-4": ("packages/zu-shadow/tests/test_conformance_audit4.py", "test_secrets_are_redacted_before_reaching_the_log"),
    # §8 — every instrument use is on the hash-chained log, bound to the consent
    # that authorized it (acted-within-authority provable from the log). The next
    # integer in the FIXED ZU-AUDIT family.
    "ZU-AUDIT-5": ("packages/zu-core/tests/test_credential_broker.py", "test_use_is_audit_bound_to_consent_and_chains"),
    "ZU-EXT-1": ("packages/zu-core/tests/test_registry.py", "test_consumer_registers_new_kind_without_core_edit"),
    "ZU-EXT-2": ("docs/TCB.md", None),
    "ZU-EXT-3": ("packages/zu-backends/tests/test_oop_channel.py", "test_channel_returns_derived_token_not_secret"),
    "ZU-EXT-4": ("packages/zu-backends/tests/test_oop_channel.py", "test_broker_secret_never_in_harness_memory"),
    # A human-rescue-derived demonstration is review-gated, never auto-promoted —
    # the apprenticeship loop reuses Shadow's promotion gate. Proof in zu-cli/tests
    # (repo-relative path resolves from _ROOT).
    "ZU-EXT-5": ("packages/zu-cli/tests/test_apprentice.py", "test_unverified_rescue_agent_is_blocked_from_promotion"),
    # ZU-RAIL — the rail mechanisms a delegated-action consumer needs.
    "ZU-RAIL-1": ("packages/zu-core/tests/test_rail.py", "test_unapproved_rail_is_refused_before_any_step"),
    "ZU-RAIL-2": ("packages/zu-core/tests/test_rail.py", "test_explore_mode_disarms_capability_bearing_call"),
    "ZU-RAIL-3": ("packages/zu-core/tests/test_rail.py", "test_arbiter_escalates_high_step_to_human"),
    "ZU-RAIL-4": ("packages/zu-core/tests/test_rail.py", "test_annotations_reach_the_replayed_tool_invoked_ctx"),
    "ZU-RAIL-5": ("packages/zu-core/tests/test_monitor.py", "test_monitor_violation_escalates_to_terminal"),
    "ZU-RAIL-6": ("packages/zu-core/tests/test_invariants.py", "test_compiled_invariant_escalates_in_loop"),
    "ZU-RAIL-7": ("packages/zu-core/tests/test_reachability.py", "test_trap_state_detected"),
    "ZU-RAIL-8": ("packages/zu-core/tests/test_rollback.py", "test_rollback_restores_state_and_replans"),
    # The proof lives in zu-patterns/tests; the matrix uses repo-relative paths
    # from _ROOT (parents[3] == repo root), so it resolves.
    "ZU-RAIL-9": ("packages/zu-patterns/tests/test_pattern_rail.py", "test_pattern_mismatch_fires_detector"),
}


def test_every_requirement_has_a_proof() -> None:
    missing: list[str] = []
    for req, (rel, symbol) in MATRIX.items():
        path = _ROOT / rel
        if not path.exists():
            missing.append(f"{req}: missing {rel}")
            continue
        if symbol is not None and symbol not in path.read_text():
            missing.append(f"{req}: {rel} has no '{symbol}'")
    assert not missing, "conformance proofs missing:\n" + "\n".join(missing)


def test_matrix_covers_all_requirement_families() -> None:
    families = {req.rsplit("-", 1)[0] for req in MATRIX}
    assert families == {"ZU-CORE", "ZU-NET", "ZU-CD", "ZU-AUDIT", "ZU-EXT", "ZU-RAIL"}
