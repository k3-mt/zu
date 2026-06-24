"""Reference WorkloadIdentity implementations (ZU-NET-4 / ZU-NET-5).

The harness presents an attestable identity on a channel; the peer verifies it;
the verified principal is recorded per action (under ``payload["ctx"]["peer"]``,
the ZU-AUDIT-3 convention). The mechanism is pluggable — this ships a stdlib-only
``StaticIdentity`` (an HMAC-signed principal assertion) as the dependency-light
reference; mTLS (stdlib ``ssl`` + a cert pair) and SPIFFE/SPIRE (a sibling
package importing the SPIRE SDK) are follow-on implementations behind the SAME
port, never in the core.

Workload identity is a *precondition* for authorization, never a substitute for
it (the consumer's grant remains the authority — ZU-NOT-1). The proof never
carries a private key. It MAY carry an attestation ``measurement`` (ZU-NET-5,
SHOULD): a verifier configured with an ``expected_measurement`` refuses a proof
whose measurement differs (so a tampered harness gets no identity); with none
configured it degrades to identity-only.
"""

from __future__ import annotations

import hashlib
import hmac
import json

from zu_core.ports import IdentityProof


class StaticIdentity:
    scheme = "static-hmac"

    def __init__(
        self,
        principal: str,
        key: str,
        *,
        measurement: str | None = None,
        trusted_keys: dict[str, str] | None = None,
        expected_measurement: str | None = None,
    ) -> None:
        # Our own identity (to present) ...
        self._principal = principal
        self._key = key
        self._measurement = measurement
        # ... and what we trust when verifying a peer: principal -> shared key.
        # Defaults to trusting our own principal (a single-host pair).
        self._trusted = dict(trusted_keys or {principal: key})
        self._expected_measurement = expected_measurement

    def _sign(self, principal: str, key: str, measurement: str | None = None) -> str:
        # Bind BOTH the principal AND (when present) the attestation measurement
        # into the signed material (ZU-NET-5). The measurement must NOT ride as
        # unsigned plaintext: it is the whole point of attestation, so an
        # intermediary that swaps it in transit (no key needed for a plaintext ==
        # compare) would defeat it. Canonical JSON of ``[principal, measurement]``
        # maps the pair 1:1 to bytes — ``["a|b", null]`` and ``["a", "b"]`` encode
        # differently, so there is no delimiter-injection ambiguity.
        msg = json.dumps([principal, measurement], separators=(",", ":"))
        return hmac.new(key.encode(), msg.encode(), hashlib.sha256).hexdigest()

    def present(self) -> IdentityProof:
        proof: dict = {"sig": self._sign(self._principal, self._key, self._measurement)}
        if self._measurement is not None:
            proof["measurement"] = self._measurement  # NET-5 attestation (now signed)
        return IdentityProof(scheme=self.scheme, principal=self._principal, proof=proof)

    def verify(self, proof: IdentityProof) -> str | None:
        if proof.scheme != self.scheme:
            return None
        key = self._trusted.get(proof.principal)
        if key is None:
            return None
        # Verify the sig over the principal AND the PRESENTED measurement, so a
        # tampered measurement breaks the signature itself — not merely a plaintext
        # equality compare a key-less intermediary could satisfy by editing both.
        presented = proof.proof.get("measurement")
        expected_sig = self._sign(proof.principal, key, presented)
        if not hmac.compare_digest(expected_sig, str(proof.proof.get("sig", ""))):
            return None
        # NET-5: if this verifier requires an attestation measurement, enforce the
        # exact value; otherwise accept identity-only (degrade gracefully).
        if self._expected_measurement is not None:
            if presented != self._expected_measurement:
                return None
        return proof.principal


__all__ = ["StaticIdentity"]
