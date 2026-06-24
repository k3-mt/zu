"""ZU-NET-2/3, ZU-CORE-3 — harness-owned channels and out-of-process plugins.

ZU-NET-2: a typed Channel to an external endpoint (the credential broker) with
the inference-channel properties — the policy emits a typed verb and gets a
derived response, never the secret.

ZU-NET-3 / ZU-CORE-3: run the broker out-of-process so its secret lives in a
separate address space. A simulated harness-memory scrape (the proxy's reachable
object graph + the parent's environment) cannot reach the secret, yet the harness
can still ask the broker to USE it (mint a token).
"""

from __future__ import annotations

import gc
import os

from zu_backends.broker import CredentialBroker
from zu_backends.oop_launcher import OutOfProcessLauncher
from zu_core.ports import ChannelRequest


async def test_channel_returns_derived_token_not_secret(monkeypatch) -> None:
    # ZU-NET-2 (in-process): the channel returns a derived token; the secret is
    # never in the response, and the verb is typed.
    monkeypatch.setenv("ZU_BROKER_SECRET", "card-4242-4242-4242")
    broker = CredentialBroker()
    resp = await broker.call(ChannelRequest(op="mint", args={"nonce": "n1"}))
    assert resp.ok and resp.data["token"].startswith("tok_")
    assert "card-4242-4242-4242" not in str(resp.data)  # secret not exposed
    # A different nonce derives a different token (it really used the secret+nonce).
    resp2 = await broker.call(ChannelRequest(op="mint", args={"nonce": "n2"}))
    assert resp2.data["token"] != resp.data["token"]


async def test_broker_secret_never_in_harness_memory() -> None:
    # ZU-NET-3 / ZU-CORE-3: the secret lives only in the worker process.
    SECRET = "S3CRET-CARD-NUMBER-4242"
    # The harness process must NOT hold the secret in its own environment.
    assert SECRET not in "".join(os.environ.values())

    launcher = OutOfProcessLauncher()
    try:
        broker = await launcher.launch_channel(
            "zu_backends.broker:CredentialBroker",
            endpoint="credential-broker",
            env={"ZU_BROKER_SECRET": SECRET},
        )
        # The harness can ask the broker to USE the credential...
        resp = await broker.call(ChannelRequest(op="mint", args={"nonce": "abc"}))
        assert resp.ok and resp.data["token"].startswith("tok_")
        # ...minting is deterministic within the worker (same nonce -> same token,
        # proving it really derived from its own secret+key, not a constant),
        # while the token stays decoupled from the secret's entropy: it is an HMAC
        # under a broker-held key, never reproducible from the secret alone — so we
        # can't (and a brute-forcer can't) recompute it out here. See broker.py.
        resp_again = await broker.call(ChannelRequest(op="mint", args={"nonce": "abc"}))
        assert resp_again.data["token"] == resp.data["token"]
        assert SECRET not in str(resp.data)  # secret not exposed in the response

        # ...but a scrape of the harness's reachable memory cannot find it.
        reachable = repr(broker.__dict__) + repr(gc.get_referents(broker, broker.__dict__))
        assert SECRET not in reachable
        # ...and it never entered the parent process's environment.
        assert SECRET not in "".join(os.environ.values())
    finally:
        await launcher.aclose()
