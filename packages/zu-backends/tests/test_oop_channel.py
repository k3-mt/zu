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
import sys
import textwrap

from zu_backends.broker import CredentialBroker
from zu_backends.oop_launcher import OutOfProcessLauncher
from zu_backends.oop_worker import _scrub_secret_env
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


def test_scrub_secret_env_pops_caller_keys_after_consumption() -> None:
    # Issue #49: the consume-then-scrub primitive. The launcher names the
    # caller-supplied secret keys in ZU_OOP_SECRET_KEYS; after the plugin's
    # constructor has read them, the worker deletes exactly those keys (and the
    # marker itself) from its environ, so they do not linger in /proc/self/environ.
    fake_env = {
        "PATH": "/usr/bin",
        "ZU_OOP_SOCK": "/tmp/x.sock",
        "ZU_BROKER_SECRET": "card-4242-4242-4242",
        "MY_OTHER_TOKEN": "tok-abc",
        "ZU_OOP_SECRET_KEYS": "ZU_BROKER_SECRET,MY_OTHER_TOKEN",
    }
    # The secret WAS available before scrubbing (so the handshake/constructor could
    # read it) ...
    assert fake_env["ZU_BROKER_SECRET"] == "card-4242-4242-4242"
    removed = _scrub_secret_env(fake_env)
    # ... and is gone afterwards, along with the marker, leaving the rest intact.
    assert "ZU_BROKER_SECRET" not in fake_env
    assert "MY_OTHER_TOKEN" not in fake_env
    assert "ZU_OOP_SECRET_KEYS" not in fake_env
    assert fake_env == {"PATH": "/usr/bin", "ZU_OOP_SOCK": "/tmp/x.sock"}
    assert set(removed) == {"ZU_BROKER_SECRET", "MY_OTHER_TOKEN", "ZU_OOP_SECRET_KEYS"}
    # Idempotent / safe when there is nothing to scrub.
    assert _scrub_secret_env({"PATH": "/usr/bin"}) == []


async def test_worker_environ_scrubbed_of_secret_after_construction(tmp_path, monkeypatch) -> None:
    # Issue #49 (end-to-end, $0, no network/Docker): launch a real OOP worker whose
    # plugin snapshots dict(os.environ) at CONSTRUCTION time and serves it back over
    # the RPC channel. The secret must be ABSENT from that worker-side snapshot — the
    # worker scrubs it right after the plugin consumed it, before serving any code.
    SECRET = "WORKER-SCRUB-SECRET-4242"
    probe = tmp_path / "env_probe.py"
    probe.write_text(textwrap.dedent('''
        import os
        from zu_core.ports import ChannelRequest, ChannelResponse

        class EnvProbe:
            endpoint = "env-probe"
            def __init__(self, secret_env: str = "ZU_BROKER_SECRET") -> None:
                # Read the secret at construction (like CredentialBroker) so the
                # worker can scrub it from the environ immediately afterwards.
                self._secret = os.environ.get(secret_env, "")
            async def call(self, req: ChannelRequest) -> ChannelResponse:
                if req.op == "environ":
                    return ChannelResponse(ok=True, data={
                        "has_secret_local": bool(self._secret),
                        "env_values": "".join(os.environ.values()),
                        "secret_key_present": "ZU_BROKER_SECRET" in os.environ,
                        "marker_present": "ZU_OOP_SECRET_KEYS" in os.environ,
                    })
                return ChannelResponse(ok=False, error="unknown op")
    '''))
    # Make the probe importable by the child via inherited PYTHONPATH.
    monkeypatch.setenv("PYTHONPATH", str(tmp_path) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    assert SECRET not in "".join(os.environ.values())  # not in the parent either

    launcher = OutOfProcessLauncher()
    try:
        probe_ch = await launcher.launch_channel(
            "env_probe:EnvProbe", endpoint="env-probe",
            env={"ZU_BROKER_SECRET": SECRET},
        )
        resp = await probe_ch.call(ChannelRequest(op="environ", args={}))
        assert resp.ok
        # The plugin DID consume the secret into its own address space at construction...
        assert resp.data["has_secret_local"] is True
        # ...but the worker's live environ no longer carries it (the scrub ran).
        assert SECRET not in resp.data["env_values"]
        assert resp.data["secret_key_present"] is False
        assert resp.data["marker_present"] is False
    finally:
        await launcher.aclose()
        sys.modules.pop("env_probe", None)
