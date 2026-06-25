"""The reference in-memory CredentialBroker (§8) — scoped/audited USE of an
instrument WITHOUT the policy ever holding the secret.

Alongside ``grants.py``/``ledger.py``: the established "the in-memory port impl
lives in zu-core" pattern. The broker is the CONTAINMENT layer, NEVER the
instrument — it holds a reference to an ``Instrument`` (which alone holds the
secret), enforces the grant mechanically, and on a full allow calls the instrument
harness-side and forwards only the outcome. zu-core imports nothing but pydantic +
stdlib; the reference instrument is a FAKE (``zu_core.instruments``); there is NO
payment SDK and NO network.

ENFORCEMENT ORDER in ``use`` (fail-closed — every refusal is logged, the instrument
is touched ONLY on a full allow):
  0. REPLAY dedupe — if this idempotency_key was already used, return the PRIOR
       outcome (consume-once, ZU-CD-6): no second charge, no second cumulative
       reservation, no duplicate CAPABILITY_USED.
  1. grant exists & not revoked       -> else refuse "revoked"
  2. operation ∈ scope.operations     -> else refuse "scope" {scope_exceeded}
  3. payee ∈ scope.payees (if set)    -> else refuse "scope" {payee_not_allowlisted}
  4. not expired(now)                 -> else refuse "expired"
  5. consent PRESENT and matching     -> else refuse "no_consent" (PRESENCE is
       enforced when the grant requires consent — an absent consent_ref is refused,
       not just a mismatch).
  6. amount ≤ per_use_limit           -> else refuse "per_use"
  7. cumulative cap, FAIL-CLOSED — a read-only PRE-CHECK refuses a use that WOULD
       exceed the cap BEFORE the instrument is charged. The atomic incr_if_below
       commit is deferred to AFTER a CAPTURED outcome (authorize→capture), so a
       DECLINE never consumes the cap and a replay never double-counts it.
  8. ALLOW: call the instrument (the ONLY place the secret is used, harness-side).
       On a CAPTURED outcome, atomically incr_if_below to COMMIT the spend, emit
       CAPABILITY_USED bound to grant + consent, return the outcome. On a DECLINE,
       commit NOTHING, emit a DEFENSE_BLOCKED (declined), return ok=False.

AUTHORIZE→CAPTURE RECONCILIATION (the spend-accounting contract). The cumulative
counter reflects only ACTUAL captures: (i) a retried use with the same
idempotency_key is deduped by the consume-once ledger — it returns the prior outcome
and takes NO new reservation; (ii) a DECLINED charge captures nothing and so commits
nothing to the cap and returns ok=False. The cap pre-check stays fail-closed (a use
that would exceed the cap is refused before the instrument is touched); the
``incr_if_below`` after capture remains the race-proof commit.

HIGH-CONSEQUENCE → HUMAN is WIRING, not a broker refusal. The broker is the pure
mechanical enforcer of scope/limit/TTL/revocation/cumulative. The high-consequence
predicate (amount > ``scope.requires_human_over``, a new payee) is owned by the
``BrokerGate`` (``broker_gate.py``), which runs BEFORE the broker tool in the loop's
``_gate_checkpoint`` and maps it to ``Verdict(kind="human")`` → the existing
``_pause_for_human``. That separation is deliberate: the broker cannot see a human
approval, so if IT refused on ``requires_human`` the gate-approved resume (which
bypasses the gate) would deadlock re-tripping the broker. The gate pauses; once a
human approves, the resumed call reaches the broker, which enforces the remaining
mechanical limits and proceeds. ``requires_human`` exposes the same predicate for a
non-loop consumer to consult directly, without making it a refusal."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from . import events as ev
from .grants import InMemoryGrantStore
from .ledger import InMemoryExecutionLedger
from .ports import (
    CredentialBroker,
    ExecutionLedger,
    Grant,
    GrantStore,
    Instrument,
    UseOutcome,
    UseRequest,
)

# The emit hook the harness/loop wires so every decision lands on the hash-chained
# log. ``(event_type, payload) -> Awaitable``; ``None`` (the default) makes the
# broker usable as a pure unit with no log (the tests that don't assert on the log
# pass ``None``; the conformance proofs pass a real emitter over a MemoryEventSink).
Emit = Callable[[str, dict], Awaitable[Any]]


async def _noop_emit(event_type: str, payload: dict) -> None:
    return None


class InMemoryCredentialBroker:
    """A reference ``CredentialBroker`` (the port in ``zu_core.ports``) enforcing
    the §8 containment contract mechanically over an ``Instrument``.

    Constructed harness-side with the instrument(s) that hold the secret and a
    ``GrantStore`` (defaults to ``InMemoryGrantStore`` so cumulative caps reuse the
    same atomic ``incr_if_below`` journal). The policy never receives the
    instrument, the instrument_ref, or the secret — only an opaque ``capability_id``
    and a ``UseOutcome``."""

    name = "memory"

    def __init__(
        self,
        instruments: dict[str, Instrument] | Instrument,
        *,
        grants: GrantStore | None = None,
        ledger: ExecutionLedger | None = None,
        emit: Emit | None = None,
    ) -> None:
        # The capability table, keyed by the OPAQUE grant id (the handle).
        self._grants: dict[str, Grant] = {}
        # instrument_ref -> the secret-holder. Accept a single instrument for the
        # common one-instrument case; key it by its own ``ref``.
        if isinstance(instruments, dict):
            self._instruments: dict[str, Instrument] = dict(instruments)
        else:
            self._instruments = {instruments.ref: instruments}
        # The atomic cumulative counter (reuses GrantStore.incr_if_below). The same
        # journal→harness.grant.updated machinery the gate uses (ZU-CD-4).
        self._store: GrantStore = grants or InMemoryGrantStore()
        # Consume-once over idempotency_key (ZU-CD-6): the atomic test-and-set that
        # makes a REPLAYED use a no-op (no second charge, no second reservation, no
        # duplicate CAPABILITY_USED). The prior outcome is returned from ``_replay``.
        self._ledger: ExecutionLedger = ledger or InMemoryExecutionLedger()
        # idempotency_key -> the outcome already produced for it, so a replay returns
        # the PRIOR outcome verbatim (the ledger.claim decides first-vs-replay; this
        # holds the value to hand back).
        self._replay: dict[str, UseOutcome] = {}
        self._emit: Emit = emit or _noop_emit

    # --- registration / revocation ----------------------------------------

    def grant(self, grant: Grant) -> str:
        """Register a :class:`Grant`; return its opaque id (the capability handle
        the policy holds)."""
        self._grants[grant.id] = grant
        return grant.id

    def revoke(self, grant_id: str) -> None:
        """Flip a grant revoked (durably, in the grant table); every subsequent
        ``use`` of the handle is refused + logged."""
        g = self._grants.get(grant_id)
        if g is not None:
            self._grants[grant_id] = g.model_copy(update={"revoked": True})

    async def emit_issued(self, grant: Grant) -> None:
        """Record a grant issuance on the log (GRANT_ISSUED). Separate from
        ``grant`` so registration stays a pure, synchronous table write the harness
        can call before it has an emit context; the harness calls this when it wants
        the issuance on the audit chain."""
        await self._emit(
            ev.GRANT_ISSUED,
            {
                "scope": {
                    "operations": sorted(grant.scope.operations),
                    "payees": sorted(grant.scope.payees) if grant.scope.payees is not None else None,
                    "requires_human_over": grant.scope.requires_human_over,
                },
                "per_use_limit": grant.per_use_limit,
                "cumulative_limit": grant.cumulative_limit,
                "ctx": {
                    "grant_id": grant.id,
                    "instrument_ref": grant.instrument_ref,
                    "consent_id": grant.consent.consent_id,
                },
            },
        )

    async def emit_revoked(self, grant_id: str) -> None:
        """Record a revocation on the log (GRANT_REVOKED)."""
        await self._emit(ev.GRANT_REVOKED, {"ctx": {"grant_id": grant_id}})

    def requires_human(self, req: UseRequest) -> bool:
        """The high-consequence predicate, computed HARNESS-SIDE from the Grant +
        the literal request (NEVER policy self-report): a use whose amount exceeds
        the grant's ``scope.requires_human_over``. The ``BrokerGate`` uses the same
        predicate to route to the human pause; a non-loop consumer can consult it
        directly. A revoked/unknown grant is not high-consequence here — it is
        refused outright by ``use``."""
        g = self._grants.get(req.capability_id)
        if g is None or g.scope.requires_human_over is None:
            return False
        return float(req.args.get("amount", 0)) > g.scope.requires_human_over

    # --- the one path the secret is touched -------------------------------

    async def use(self, req: UseRequest) -> UseOutcome:
        """Check the grant ⊆ consent + limits/TTL/revocation, and ONLY on a full
        allow perform the instrument operation USING the secret internally, binding
        the use to the consent on the audit log. Every refusal is logged
        (DEFENSE_BLOCKED) and the instrument is NEVER touched on a refusal."""
        # 0. REPLAY dedupe (consume-once, ZU-CD-6). A retried use with the SAME
        #    idempotency_key returns the PRIOR outcome verbatim and takes NO new
        #    reservation and emits NO duplicate CAPABILITY_USED. ``ledger.claim`` is
        #    the atomic test-and-set: the FIRST use of a key wins the claim (proceeds
        #    below); a later replay loses it and is short-circuited here.
        key = req.idempotency_key
        if isinstance(key, str):
            if not self._ledger.claim(key):
                prior = self._replay.get(key)
                if prior is not None:
                    return prior
                # Claimed by another path (e.g. the loop's resume claim) but no
                # cached outcome here — fail closed rather than re-charge.
                return await self._refuse(
                    req, self._grants.get(req.capability_id), "replay",
                    "replayed_use", "idempotency_key already consumed; no re-charge")
            # First claim journals (harness.execution.claimed) so resume sees it.
            await self._flush_execution_journal()
        g = self._grants.get(req.capability_id)
        # 1. exists & not revoked.
        if g is None or g.revoked:
            return await self._refuse(req, g, "revoked", "capability_revoked",
                                      "no such capability or it has been revoked")
        # 2. operation in scope.
        if req.operation not in g.scope.operations:
            return await self._refuse(req, g, "scope", "scope_exceeded",
                                      f"operation {req.operation!r} not in scope")
        # 3. payee allowlist (the off-allowlist-payee attack).
        if g.scope.payees is not None and req.args.get("payee") not in g.scope.payees:
            return await self._refuse(req, g, "scope", "payee_not_allowlisted",
                                      f"payee {req.args.get('payee')!r} not allowlisted")
        # 4. TTL / expiry.
        if g.expired(datetime.now(UTC)):
            return await self._refuse(req, g, "expired", "capability_expired",
                                      "capability past its TTL")
        # 5. consent PRESENCE + match. When the grant requires consent (the default),
        #    the policy MUST name a consent (an absent consent_ref is refused — not
        #    just a mismatch), and it must match the grant's consent (a use cannot
        #    borrow another grant's authority). A grant may opt out explicitly
        #    (requires_consent=False) — then a missing consent_ref is allowed.
        if g.requires_consent and req.consent_ref is None:
            return await self._refuse(req, g, "no_consent", "consent_absent",
                                      "this grant requires a consent_ref on every use")
        if req.consent_ref is not None and req.consent_ref != g.consent.consent_id:
            return await self._refuse(req, g, "no_consent", "consent_mismatch",
                                      "consent_ref does not match the grant's consent")
        amount = float(req.args.get("amount", 0))
        # 6. per-use limit.
        if g.per_use_limit is not None and amount > g.per_use_limit:
            return await self._refuse(req, g, "per_use", "per_use_exceeded",
                                      f"amount {amount} over per-use limit {g.per_use_limit}")
        # (HIGH-CONSEQUENCE → human is the BrokerGate's job, BEFORE this point — see
        #  the module docstring. The broker is the mechanical enforcer; it does not
        #  refuse on requires_human, so a gate-approved resume proceeds here.)
        # 7. cumulative cap — FAIL-CLOSED read-only PRE-CHECK. The atomic commit is
        #    deferred to AFTER capture (step 8) so a DECLINE/replay never consumes
        #    the cap. Here we refuse a use that WOULD exceed the cap BEFORE charging.
        if g.cumulative_limit is not None:
            current = float(self._store.get(g.id, g.cumulative_key, 0) or 0)
            if current + amount > g.cumulative_limit:
                return await self._refuse(req, g, "cumulative", "cumulative_exceeded",
                                          f"amount {amount} would exceed cumulative limit "
                                          f"{g.cumulative_limit}")
        # 8. ALLOW. The ONLY place the secret is used — harness-side, internal.
        instrument = self._instruments.get(g.instrument_ref)
        if instrument is None:
            return await self._refuse(req, g, "scope", "unknown_instrument",
                                      f"no instrument {g.instrument_ref!r} registered")
        perform_args = dict(req.args)
        if req.idempotency_key is not None:
            perform_args.setdefault("idempotency_key", req.idempotency_key)
        outcome = await instrument.perform(req.operation, perform_args)
        # Authorize→capture reconciliation: the cumulative cap reflects only what was
        # actually CAPTURED. A non-captured outcome (decline/reject) commits nothing.
        if str(outcome.get("status")) != "captured":
            result = await self._refuse_outcome(req, g, outcome)
            if isinstance(key, str):
                self._replay[key] = result
            return result
        # CAPTURED — commit the spend atomically (the race-proof check-and-increment
        # remains the real cumulative guard; the pre-check above is fail-closed but
        # not the commit). On the (concurrent) edge where the commit now exceeds the
        # cap, the charge already happened, so we still record the use truthfully.
        captured = float(outcome.get("captured", amount))
        if g.cumulative_limit is not None:
            self._store.incr_if_below(g.id, g.cumulative_key, captured, g.cumulative_limit)
            await self._flush_grant_journal()
        # Audit-bind the use to the grant + consent on the hash-chained log
        # (ZU-AUDIT-5). The OUTCOME summary only — never the secret.
        await self._emit(
            ev.CAPABILITY_USED,
            {
                "operation": req.operation,
                "outcome": outcome,
                "ctx": {
                    "grant_id": g.id,
                    "consent_id": g.consent.consent_id,
                    "capability_id": g.id,
                    "instrument_ref": g.instrument_ref,
                    "idempotency_key": req.idempotency_key,
                },
            },
        )
        result = UseOutcome(ok=True, outcome=outcome)
        if isinstance(key, str):
            self._replay[key] = result
        return result

    # --- helpers ----------------------------------------------------------

    async def _refuse(
        self, req: UseRequest, g: Grant | None, code: str, kind: str, detail: str
    ) -> UseOutcome:
        """Log a contained refusal (DEFENSE_BLOCKED) and return a refused outcome.
        The instrument is NOT touched — a refusal never reaches the secret."""
        ctx: dict[str, Any] = {"capability_id": req.capability_id}
        if g is not None:
            ctx["grant_id"] = g.id
            ctx["consent_id"] = g.consent.consent_id
        await self._emit(
            ev.DEFENSE_BLOCKED,
            {
                "kind": kind,
                "operation": req.operation,
                "refused": code,
                "detail": detail,
                "ctx": ctx,
            },
        )
        return UseOutcome(ok=False, refused=code, detail=detail)

    async def _refuse_outcome(
        self, req: UseRequest, g: Grant, outcome: dict
    ) -> UseOutcome:
        """A charge the instrument REFUSED (declined/rejected) — captured nothing, so
        it commits NOTHING to the cumulative cap. Logged as a contained
        DEFENSE_BLOCKED (NOT a success CAPABILITY_USED) and returned ``ok=False`` with
        the decline reason named, so a caller cannot mistake a decline for a charge."""
        reason = str(outcome.get("decline_reason") or outcome.get("status") or "declined")
        await self._emit(
            ev.DEFENSE_BLOCKED,
            {
                "kind": "charge_declined",
                "operation": req.operation,
                "refused": "declined",
                "detail": f"instrument declined the charge ({reason}); nothing captured",
                "ctx": {
                    "grant_id": g.id,
                    "consent_id": g.consent.consent_id,
                    "capability_id": g.id,
                    "instrument_ref": g.instrument_ref,
                    "idempotency_key": req.idempotency_key,
                },
            },
        )
        return UseOutcome(ok=False, refused="declined", detail=reason, outcome=outcome)

    async def _flush_execution_journal(self) -> None:
        """Drain the ExecutionLedger journal onto the log as harness.execution.claimed
        so a consumed idempotency_key survives pause/resume (ZU-CD-6). A durable
        backing has no journal; the drain is then a no-op."""
        drain = getattr(self._ledger, "drain", None)
        if drain is None:
            return
        for execution_key in drain():
            await self._emit(
                ev.EXECUTION_CLAIMED,
                {"key": execution_key, "ctx": {"idempotency_key": execution_key}},
            )

    async def _flush_grant_journal(self) -> None:
        """Drain the GrantStore journal onto the log as harness.grant.updated so a
        cumulative counter survives pause/resume (ZU-CD-4). A durable backing store
        has no journal; the drain is then a no-op."""
        drain = getattr(self._store, "drain", None)
        if drain is None:
            return
        for grant_id, key, value in drain():
            await self._emit(
                ev.GRANT_UPDATED,
                {"grant_id": grant_id, "key": key, "value": value,
                 "ctx": {"grant_id": grant_id}},
            )


# A structural-conformance assertion at import time would be heavy; the proof
# tests assert ``isinstance(broker, CredentialBroker)`` over the runtime_checkable
# Protocol instead. Re-export the name so callers can ``from zu_core.broker import
# InMemoryCredentialBroker``.
_: type[CredentialBroker] = InMemoryCredentialBroker

__all__ = ["InMemoryCredentialBroker"]
