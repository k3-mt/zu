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
"""

from __future__ import annotations

import hashlib
import os

from zu_core.ports import ChannelRequest, ChannelResponse


class CredentialBroker:
    endpoint = "credential-broker"

    def __init__(self, secret_env: str = "ZU_BROKER_SECRET") -> None:
        # Resolved inside the adapter, never placed in the policy's context or a
        # config file — the same discipline as a provider's API key.
        self._secret = os.environ.get(secret_env, "")

    async def call(self, req: ChannelRequest) -> ChannelResponse:
        if req.op == "mint":
            # A token DERIVED from the secret (one-way), so possession of the
            # token never reveals the secret.
            nonce = str(req.args.get("nonce", ""))
            digest = hashlib.sha256(f"{self._secret}:{nonce}".encode()).hexdigest()
            return ChannelResponse(ok=True, data={"token": "tok_" + digest[:24]})
        if req.op == "introspect":
            return ChannelResponse(ok=True, data={"has_secret": bool(self._secret)})
        return ChannelResponse(ok=False, error=f"unknown op: {req.op}")


__all__ = ["CredentialBroker"]
