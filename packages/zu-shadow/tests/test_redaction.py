"""Redaction-before-append: secrets fed in must be ABSENT from what reaches the log."""

from __future__ import annotations

import json

from zu_core.bus import EventBus
from zu_core.contracts import Event
from zu_shadow.capture import SemanticTarget
from zu_shadow.recorder import RawInput, Recorder
from zu_shadow.redaction import REDACTED, RedactionPolicy, redact_event, redact_text

# Concrete secrets the test feeds in; NONE may appear on the log.
PASSWORD = "hunter2-SUPER-secret"
BEARER = "Bearer abcdEFGH12345678ZZZ"
COOKIE = "session=DEADBEEFtopsecretcookievalue"
APIKEY = "sk-livedEADbeefDEADbeef0000"
JWT = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.sIgnAtUreSecretXYZ"


def _all_secrets() -> list[str]:
    return [PASSWORD, BEARER.split()[1], COOKIE, APIKEY, JWT]


def test_redact_text_strips_token_shapes() -> None:
    text = f"login with {APIKEY} and {BEARER} token {JWT}"
    out = redact_text(text)
    for secret in (APIKEY, BEARER.split()[1], JWT):
        assert secret not in out
    assert REDACTED in out


def test_redact_event_blanks_credential_field_and_headers() -> None:
    e = Event(
        trace_id=__import__("uuid").uuid4(),
        task_id=__import__("uuid").uuid4(),
        type="data.shadow.user.type",
        source="t",
        payload={
            "target": {"role": "textbox", "name": "Password", "label": "Password"},
            "password": PASSWORD,
            "headers": {"Authorization": BEARER, "Cookie": COOKIE, "X-Trace": "ok"},
        },
    )
    red = redact_event(e)
    assert isinstance(red, Event)
    blob = json.dumps(red.payload)
    for secret in _all_secrets():
        assert secret not in blob, f"{secret!r} leaked"
    assert red.payload["password"] == REDACTED
    assert red.payload["headers"]["Authorization"] == REDACTED
    assert red.payload["headers"]["Cookie"] == REDACTED
    assert red.payload["headers"]["X-Trace"] == "ok"  # a non-secret header survives


async def test_secrets_never_reach_the_sink() -> None:
    """The end-to-end guarantee: feed secrets through the recorder, then read the
    sink (the append-only log) and assert NO secret is present anywhere."""
    bus = EventBus()
    rec = Recorder(bus, site="https://shop.example.com")
    stream = [
        RawInput(kind="navigate", url=f"https://shop.example.com/login?token={APIKEY}",
                 intent=f"I paste my key {APIKEY} here"),
        RawInput(kind="type",
                 target=SemanticTarget(role="textbox", name="Password", label="Password"),
                 value=PASSWORD, intent="type the password"),
        RawInput(kind="network", url="https://api.shop.example.com/auth", status=200,
                 host="api.shop.example.com",
                 headers={"Set-Cookie": COOKIE}),  # genuinely inject the cookie
    ]
    await rec.record_stream(stream, outcome="logged in")

    logged = await bus.query()  # what actually reached the append-only sink
    blob = json.dumps([{"type": e.type, "payload": e.payload} for e in logged])
    for secret in _all_secrets():
        assert secret not in blob, f"{secret!r} reached the log — redaction did not precede append"
    # The "why" narration that embedded a key is redacted too.
    nav = next(e for e in logged if e.type == "data.shadow.user.navigate")
    assert APIKEY not in json.dumps(nav.payload)
    await bus.aclose()


def test_consumer_pii_pattern_is_applied() -> None:
    policy = RedactionPolicy(pii_patterns=(r"CUST-\d{6}",))
    out = redact_text("customer CUST-123456 called", policy)
    assert "CUST-123456" not in out
    assert REDACTED in out
