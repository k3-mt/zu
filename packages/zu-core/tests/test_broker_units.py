"""Unit-level proofs for the §8 broker pieces: the data model semantics, the fake
instruments, and the registry-group discovery. (The full ZU-CD/ZU-AUDIT conformance
proofs live in test_credential_broker.py.)"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from zu_core.broker import InMemoryCredentialBroker
from zu_core.instruments import FakeCardInstrument, FakeVaultInstrument
from zu_core.ports import (
    INTERFACE_VERSION,
    CapScope,
    Consent,
    Grant,
    Instrument,
    UseRequest,
)
from zu_core.registry import GROUPS, Registry, credential_broker


def _grant(**kw) -> Grant:
    return Grant(
        instrument_ref="card:fake-001",
        scope=CapScope(operations=frozenset({"charge"})),
        consent=Consent(consent_id="c1", by="alice", authority="appr"),
        **kw,
    )


# --- Grant / CapScope semantics (step 1) ----------------------------------


def test_grant_id_is_an_opaque_handle_and_grant_is_frozen() -> None:
    g = _grant()
    assert isinstance(g.id, str) and len(g.id) >= 16  # opaque, non-empty
    # frozen: the issued capability is immutable; revoke is store state, not a mutation.
    with pytest.raises(ValidationError):
        g.revoked = True


def test_grant_expiry_is_a_pure_function_of_time() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    g = _grant(ttl_s=60, created_at=created)
    assert g.expired(created + timedelta(seconds=30)) is False  # within TTL
    assert g.expired(created + timedelta(seconds=61)) is True  # past TTL
    # ttl_s=None ⇒ never expires.
    assert _grant(ttl_s=None, created_at=created).expired(created + timedelta(days=3650)) is False


def test_capscope_membership() -> None:
    scope = CapScope(operations=frozenset({"charge"}), payees=frozenset({"acct_a"}))
    assert "charge" in scope.operations and "transfer" not in scope.operations
    assert scope.payees is not None and "acct_a" in scope.payees and "acct_b" not in scope.payees


# --- the fake instruments (step 4) ----------------------------------------


async def test_fake_card_charges_and_never_returns_the_pan() -> None:
    card = FakeCardInstrument(pan="SENTINEL-PAN")
    out = await card.perform("charge", {"amount": 50})
    assert out["charge_id"] == "fake-1" and out["captured"] == 50
    assert "SENTINEL-PAN" not in repr(out)  # the secret is NOT in the outcome
    # idempotency: a retried charge with the same key does not double-increment.
    a = await card.perform("charge", {"amount": 70, "idempotency_key": "k1"})
    b = await card.perform("charge", {"amount": 70, "idempotency_key": "k1"})
    assert a == b and card.captured_total == 50 + 70
    assert isinstance(card, Instrument)


async def test_fake_vault_returns_a_derived_token_not_the_secret() -> None:
    vault = FakeVaultInstrument(root_secret="ROOT-SENTINEL")
    out = await vault.perform("issue_token", {"subject": "u", "scope": "read"})
    assert len(out["token"]) == 16 and "ROOT-SENTINEL" not in repr(out)


# --- registry-group discovery (step 2) ------------------------------------


def test_credential_brokers_is_a_discoverable_kind() -> None:
    assert GROUPS["credential_brokers"] == "zu.credential_brokers"
    assert INTERFACE_VERSION["credential_brokers"] == 1
    reg = Registry()
    assert "credential_brokers" in reg.kinds()
    broker = InMemoryCredentialBroker(FakeCardInstrument())
    reg.register("credential_brokers", "memory", broker)
    assert reg.get("credential_brokers", "memory") is broker


def test_credential_broker_decorator_registers_in_process() -> None:
    # The @credential_broker decorator mirrors @monitor/@pattern (one code path).
    @credential_broker
    class _MyBroker(InMemoryCredentialBroker):
        name = "decorated"

    from zu_core.registry import REGISTRY

    assert "decorated" in REGISTRY.names("credential_brokers")


def test_requires_human_is_computed_from_the_grant_not_self_report() -> None:
    card = FakeCardInstrument()
    broker = InMemoryCredentialBroker(card)
    g = Grant(
        instrument_ref=card.ref,
        scope=CapScope(operations=frozenset({"charge"}), requires_human_over=100.0),
        consent=Consent(consent_id="c1", by="alice", authority="appr"),
    )
    broker.grant(g)
    assert broker.requires_human(UseRequest(capability_id=g.id, operation="charge", args={"amount": 150})) is True
    assert broker.requires_human(UseRequest(capability_id=g.id, operation="charge", args={"amount": 50})) is False
