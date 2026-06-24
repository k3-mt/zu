"""A reference credential broker as a harness-owned Channel (ZU-NET-2).

This is the motivating case for the generalised channel: the harness owns a typed
channel to an external secret-holder, exactly as it owns the inference channel.
The secret is read INSIDE the adapter (from the environment, like a provider's
API key) and is never returned to or readable by the policy — the policy emits a
typed ``ChannelRequest`` verb (``mint``/``introspect``) and gets a
``ChannelResponse`` carrying a *derived* token, never the underlying secret.

Run in-process it gives the channel shape and ownership; run out-of-process
(``zu_backends.oop_launcher``) the secret additionally lives in a separate
address space, so a harness compromise cannot exfiltrate it (ZU-NET-3). The verbs
are deliberately NARROW (mint/introspect), not a generic "send this request"
proxy — narrowness is what keeps the gate able to pattern-match invocations
against a grant (ZU-EXT-3).

Token derivation: the minted token is an **HMAC under a high-entropy key minted
at construction that never leaves the broker** — *not* a bare hash of the secret.
This matters for **low-entropy** secrets (a card PAN, a PIN, a short password): a
bare ``sha256(secret:nonce)`` is brute-forceable offline because an attacker can
enumerate the small secret space and recompute the digest. HMAC'ing under a
broker-held key decouples token strength from the secret's entropy — an attacker
would have to guess the 256-bit key, not the secret — so the policy may even
control the ``nonce`` (it is just the HMAC message) without weakening the token.
Possession of a token is one-way for a secret of *any* entropy.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets

from zu_core.ports import ChannelRequest, ChannelResponse


class CredentialBroker:
    endpoint = "credential-broker"

    def __init__(self, secret_env: str = "ZU_BROKER_SECRET") -> None:
        # Resolved inside the adapter, never placed in the policy's context or a
        # config file — the same discipline as a provider's API key.
        self._secret = os.environ.get(secret_env, "")
        # A high-entropy key minted at construction that NEVER leaves the broker.
        # Tokens are HMAC'd under this key so a low-entropy secret (PAN/PIN/short
        # password) cannot be recovered offline from an observed token: brute
        # force would have to guess this 256-bit key, not the secret's small
        # input space. (See the module docstring.)
        self._mint_key = secrets.token_bytes(32)

    async def call(self, req: ChannelRequest) -> ChannelResponse:
        if req.op == "mint":
            # A token DERIVED from the secret (one-way) AND decoupled from the
            # secret's entropy: HMAC under the broker-held key, not a bare hash.
            # The policy-supplied nonce is just the HMAC message; controlling it
            # reveals neither the secret nor the key.
            nonce = str(req.args.get("nonce", ""))
            mac = hmac.new(
                self._mint_key, f"{self._secret}:{nonce}".encode(), hashlib.sha256
            ).hexdigest()
            return ChannelResponse(ok=True, data={"token": "tok_" + mac[:24]})
        if req.op == "introspect":
            return ChannelResponse(ok=True, data={"has_secret": bool(self._secret)})
        return ChannelResponse(ok=False, error=f"unknown op: {req.op}")


__all__ = ["CredentialBroker"]
