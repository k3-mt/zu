"""ZU-AUDIT-4 — secrets are redacted at capture, BEFORE any event reaches the log.

The named conformance proof. A Shadow recording IS the event bus run over a human
session, so the raw stream is saturated with the human's secrets. The guarantee is
that NONE of them reach the append-only log: redaction runs inside the recorder,
before ``EventBus.publish`` (the only caller of ``EventSink.append``), so the secret
is gone before the event is hashed into the chain — not scrubbed after.
"""

from __future__ import annotations

import json

from zu_core.bus import EventBus
from zu_shadow.capture import SemanticTarget
from zu_shadow.recorder import RawInput, Recorder

# Distinct secret materials, one per channel a human session leaks them through.
_PASSWORD = "hunter2-SUPER-secret-pw"
_API_KEY = "sk-live000DEADbeefDEADbeef"
_BEARER_TOKEN = "abcdEFGH12345678ZZZqqq"
_COOKIE = "session=TOPsecretCOOKIEvalue9999"
_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJhZG1pbiJ9.SiGNatureSecretAAA"


async def test_secrets_are_redacted_before_reaching_the_log() -> None:
    """Feed every secret channel through the recorder; assert NONE reaches the sink."""
    bus = EventBus()
    rec = Recorder(bus, site="https://portal.example.com")
    stream = [
        # A token in a navigation URL + a "why" note that pastes the key.
        RawInput(kind="navigate",
                 url=f"https://portal.example.com/in?access_token={_API_KEY}",
                 intent=f"I sign in with my key {_API_KEY}"),
        # A password typed into a credential field (marked by its semantic target).
        RawInput(kind="type",
                 target=SemanticTarget(role="textbox", name="Password", label="Password"),
                 value=_PASSWORD),
        # A bearer token + JWT pasted into a free-text field.
        RawInput(kind="type",
                 target=SemanticTarget(role="textbox", name="Notes", label="Notes"),
                 value=f"Bearer {_BEARER_TOKEN} and {_JWT}"),
        # A real CDP network response whose headers carry a Set-Cookie + Authorization.
        # The recorder captures network events as metadata-only, so these headers are
        # DROPPED at source and never enter the log — proved non-vacuously below.
        RawInput(kind="network", url="https://api.portal.example.com/me", status=200,
                 host="api.portal.example.com",
                 headers={"Set-Cookie": _COOKIE, "Authorization": f"Bearer {_BEARER_TOKEN}"}),
    ]
    await rec.record_stream(stream, outcome="signed in")

    # Read the SINK (the append-only source of truth), not the recorder's inputs.
    logged = await bus.query()
    blob = json.dumps([{"type": e.type, "payload": e.payload} for e in logged])
    for secret in (_PASSWORD, _API_KEY, _BEARER_TOKEN, _COOKIE, _JWT):
        assert secret not in blob, (
            f"{secret!r} reached the append-only log — redaction did NOT precede append"
        )
    # The cookie was genuinely present in the raw stream; assert the network event is
    # metadata-only ({url,status,host}) so the absence above is not vacuous.
    net = next(e for e in logged if e.type.endswith("network.response"))
    assert set(net.payload) == {"url", "status", "host"}
    assert "headers" not in net.payload and "Set-Cookie" not in json.dumps(net.payload)
    await bus.aclose()
