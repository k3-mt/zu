"""ZU-NET-4/5 — workload identity: present/verify, attestation, peer on the log.

The harness presents an attestable identity; the peer verifies it; a tampered
proof is rejected; an attestation measurement is enforced when required (NET-5);
and a verified peer principal is recordable on the event log and queryable
(ties to ZU-AUDIT-3).
"""

from __future__ import annotations

from uuid import uuid4

from zu_backends.identity import StaticIdentity
from zu_core.bus import EventBus
from zu_core.contracts import Event
from zu_core.eventstore import register_event_filter
from zu_core.ports import IdentityProof


def test_present_verify_roundtrip() -> None:
    ident = StaticIdentity("agent://vet", key="shared-secret")
    assert ident.verify(ident.present()) == "agent://vet"


def test_tampered_proof_is_rejected() -> None:
    ident = StaticIdentity("agent://vet", key="shared-secret")
    proof = ident.present()
    forged = IdentityProof(scheme=proof.scheme, principal=proof.principal, proof={"sig": "deadbeef"})
    assert ident.verify(forged) is None


def test_unknown_principal_is_rejected() -> None:
    # A verifier only trusts keys it was given for known principals.
    verifier = StaticIdentity("server", key="k-server", trusted_keys={"server": "k-server"})
    attacker = StaticIdentity("attacker", key="k-attacker")
    assert verifier.verify(attacker.present()) is None


def test_attestation_measurement_enforced_when_required() -> None:
    # NET-5: a verifier requiring a measurement refuses a proof whose measurement
    # differs (a tampered harness gets no identity) and accepts the matching one.
    good = StaticIdentity("agent", key="k", measurement="sha256:abc")
    verifier = StaticIdentity(
        "agent", key="k", trusted_keys={"agent": "k"}, expected_measurement="sha256:abc"
    )
    assert verifier.verify(good.present()) == "agent"

    tampered = StaticIdentity("agent", key="k", measurement="sha256:EVIL")
    assert verifier.verify(tampered.present()) is None


def test_measurement_tampering_breaks_the_signature() -> None:
    # ZU-NET-5: the attestation measurement is bound INTO the signature, so swapping
    # it on a genuine proof (no re-signing) no longer verifies — it is tamper-evident,
    # not an unsigned plaintext == compare. Mirrors the reported repro (#26).
    honest = StaticIdentity("vault", key="k", measurement="genuine-v1")
    proof = honest.present()
    proof.proof["measurement"] = "anything-i-want"  # swap measurement, keep the sig
    forger = StaticIdentity(
        "vault", key="k", trusted_keys={"vault": "k"}, expected_measurement="anything-i-want"
    )
    assert forger.verify(proof) is None  # the broken signature is detected


def test_degrades_to_identity_only_without_expected_measurement() -> None:
    # With no expected measurement configured, identity verifies without one.
    no_measure = StaticIdentity("agent", key="k")
    verifier = StaticIdentity("agent", key="k", trusted_keys={"agent": "k"})
    assert verifier.verify(no_measure.present()) == "agent"


async def test_verified_peer_is_recorded_and_queryable() -> None:
    # The verified principal lands under payload["ctx"]["peer"] and is queryable
    # (ZU-NET-4 attribution via ZU-AUDIT-3).
    register_event_filter("peer")
    ident = StaticIdentity("agent://vet", key="k")
    principal = ident.verify(ident.present())

    bus = EventBus()
    tid = uuid4()
    await bus.publish(
        Event(
            trace_id=tid, task_id=tid, type="harness.tool.returned", source="loop",
            payload={"tool": "wire_transfer", "ctx": {"peer": principal}},
        )
    )
    rows = await bus.query({"peer": "agent://vet"})
    assert len(rows) == 1 and rows[0].payload["ctx"]["peer"] == "agent://vet"
