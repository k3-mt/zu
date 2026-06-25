"""Reference FAKE instruments — the in-memory doubles behind the Instrument seam (§8).

An ``Instrument`` (the port in ``zu_core.ports``) is the pluggable issuer/vault: it
ALONE holds the secret and performs the real operation, returning only the OUTCOME.
These fakes live in zu-core — alongside ``grants.py``/``ledger.py``, the established
"the in-memory port impl lives in zu-core" pattern — so the offline conformance
proofs have NO third-party API, NO network, and NO real secret.

CONTAIN, NEVER ISSUE. These do not issue cards, move money, or do KYC. A
``FakeCardInstrument`` increments a counter and hands back a charge id; a
``FakeVaultInstrument`` derives a token from a root secret. A real issuer/vault is
a FUTURE adapter satisfying the SAME ``Instrument`` shape, in a sibling package,
behind the broker — NEVER in zu-core (which imports nothing but pydantic + stdlib).

The secret (``_pan`` / ``_root_secret``) is a PRIVATE attribute, resolved from the
constructor (the harness/operator passes it, env-resolved at construction the way a
``ModelProvider`` resolves its key). It is asserted-absent from every event/outcome
by the ZU-CD-7 proof — the secret never crosses back to the broker or the policy.
"""

from __future__ import annotations

import hashlib
import hmac


class FakeCardInstrument:
    """A fake payment card: ``perform("charge", {"amount", "payee"})`` increments
    an internal counter and returns a charge id. The PAN is the "secret" — a
    private attribute that NEVER appears in the returned outcome (the broker
    forwards only that outcome; the ZU-CD-7 proof asserts the PAN is on no event).

    ``idempotency_key`` (passed through ``args``) dedupes a retried charge: the same
    key returns the same charge id without re-incrementing the captured total, so a
    retry storm under a velocity spike cannot double-charge.

    A charge can be DECLINED (the authorize→capture distinction a real issuer
    draws): an issuer may refuse a charge (insufficient funds, fraud hold) and then
    NOTHING is captured. The fake models this with ``decline_payees`` (a charge to a
    payee in that set is declined) and a ``decline_next``/``decline_amounts`` knob,
    so the broker's authorize→capture reconciliation can be proven offline. A
    declined charge returns ``status: "declined"``, captures nothing, and does NOT
    advance ``captured_total`` — so a decline must never consume a cumulative cap."""

    def __init__(
        self,
        ref: str = "card:fake-001",
        *,
        pan: str = "4111-1111-1111-1111",
        decline_payees: frozenset[str] | None = None,
        decline_amounts: frozenset[float] | None = None,
    ) -> None:
        self.ref = ref
        # The SECRET. Private; resolved from the constructor (env-resolved by the
        # harness in production). Never exposed, never returned, never logged.
        self._pan = pan
        self._counter = 0
        self._captured_total = 0.0
        # idempotency_key -> the outcome already produced for it (dedupe a retry).
        self._seen: dict[str, dict] = {}
        # Decline knobs (test/operator only): a charge whose payee or amount is in
        # these sets is DECLINED — captures nothing. Models an issuer refusal.
        self._decline_payees: frozenset[str] = decline_payees or frozenset()
        self._decline_amounts: frozenset[float] = decline_amounts or frozenset()

    def _declined(self, args: dict, amount: float) -> bool:
        payee = args.get("payee")
        return (isinstance(payee, str) and payee in self._decline_payees) or (
            amount in self._decline_amounts
        )

    async def perform(self, operation: str, args: dict) -> dict:
        if operation != "charge":
            return {"error": f"unsupported operation {operation!r}", "status": "rejected"}
        key = args.get("idempotency_key")
        if isinstance(key, str) and key in self._seen:
            # A retried charge: same outcome, no double-increment (idempotent).
            return dict(self._seen[key])
        amount = float(args.get("amount", 0))
        # USE the secret internally — here, just to "authorize" the charge. The PAN
        # is touched harness-side and stays here; only the outcome crosses back.
        _ = self._pan  # the secret authorizes the charge; it never leaves this scope
        if self._declined(args, amount):
            # The issuer refused: NOTHING is captured (captured_total unchanged). A
            # declined outcome is still idempotent (a retried decline stays declined).
            outcome = {
                "charge_id": None,
                "captured": 0.0,
                "status": "declined",
                "decline_reason": "issuer_declined",
            }
            if isinstance(key, str):
                self._seen[key] = dict(outcome)
            return outcome
        self._counter += 1
        self._captured_total += amount
        outcome = {
            "charge_id": f"fake-{self._counter}",
            "captured": amount,
            "status": "captured",
        }
        if isinstance(key, str):
            self._seen[key] = dict(outcome)
        return outcome

    # Test/operator inspection only — NOT a policy-facing surface.
    @property
    def captured_total(self) -> float:
        return self._captured_total


class FakeVaultInstrument:
    """A fake vault/KMS: ``perform("issue_token", {...})`` returns a token DERIVED
    from a root secret (HMAC over the args), which the broker uses internally and
    does NOT hand back verbatim. The root secret is private and never leaves.

    The derived token is a truncated HMAC digest — it is a function of the secret
    but does not reveal it; the ZU-CD-7 proof asserts the root secret appears on no
    event/outcome."""

    def __init__(self, ref: str = "vault:fake-001", *, root_secret: str = "root-sentinel-secret") -> None:
        self.ref = ref
        self._root_secret = root_secret  # the SECRET; never exposed/returned/logged

    async def perform(self, operation: str, args: dict) -> dict:
        if operation != "issue_token":
            return {"error": f"unsupported operation {operation!r}", "status": "rejected"}
        material = f"{args.get('subject', '')}:{args.get('scope', '')}".encode()
        token = hmac.new(self._root_secret.encode(), material, hashlib.sha256).hexdigest()[:16]
        return {"token": token, "status": "issued"}


__all__ = ["FakeCardInstrument", "FakeVaultInstrument"]
