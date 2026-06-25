"""The DEFAULT-ON capture-time redaction stage (ZU-AUDIT-4).

The whole premise of Shadow is that a *human* drives the captured session, so the
raw input/CDP stream is saturated with the human's secrets: the password they
typed, the ``Authorization``/``Cookie`` headers their browser sent, the API token
in a URL. **None of it may ever reach the append-only log.** This module is the
stage that guarantees that — and it runs BEFORE :meth:`EventSink.append`, so the
secret is gone before the event is hashed into the chain, never merely scrubbed
after the fact.

The discipline is structural, not heuristic-first: a ``data.shadow.user.type``
event NEVER carries a credential value (the recorder marks credential fields and
this stage blanks them), and known secret headers are dropped wholesale. On top of
that floor sits a configurable, generic pattern sweep (token shapes + consumer PII
regexes) applied to every remaining string — INCLUDING the human's "why" intent
narration, which is free text and so the most likely place a secret leaks.

Pure: stdlib + pydantic only, no model, no I/O. The redactor is a pure function
``event_in -> redacted_event_out``; the recorder pipes every event through it on
the way to the sink, and a named conformance proof feeds it secrets and asserts
they are ABSENT from what reaches the log.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from pydantic import BaseModel, Field

# The placeholder a redacted value is replaced with — visible on the log so a
# reviewer sees that redaction HAPPENED (an absence would be ambiguous).
REDACTED = "[REDACTED]"

# Header names whose VALUES are secrets by definition — dropped wholesale, never
# pattern-matched (a cookie is opaque; you cannot regex it safely). Case-folded.
_SECRET_HEADERS: frozenset[str] = frozenset({"authorization", "cookie", "set-cookie",
                                             "proxy-authorization", "x-api-key"})

# Field names (in a captured user.type / form payload) the recorder may mark as a
# credential; their value is blanked regardless of content. Case-folded substring.
_CREDENTIAL_FIELD_HINTS: tuple[str, ...] = ("password", "passwd", "secret", "token",
                                            "api_key", "apikey", "otp", "cvv", "pin")

# Generic token/secret SHAPES — never a site-specific constant. Each is a class of
# high-entropy credential string that should never be on the log even if it slips
# past the structural floor (e.g. pasted into a "why" note).
_TOKEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    # Bearer/authorization inline ("Authorization: Bearer abc...", "Bearer abc...").
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"),
    # Common provider key prefixes (sk-, pk-, ghp_, xox[bap]-, AKIA…, AIza…).
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9]{12,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{12,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{20,}"),
    # JWTs (three dot-separated base64url segments).
    re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+"),
    # A token-bearing query/credential parameter (?token=…, &access_token=…,
    # api_key=…, password=…) — value blanked, key kept so the shape is auditable.
    re.compile(r"(?i)\b(token|access_token|api_key|apikey|key|password|secret|sig)=[^&\s\"']+"),
    # userinfo credentials in a URL (https://user:pass@host).
    re.compile(r"(?i)(https?://)[^/@\s:]+:[^/@\s]+@"),
)


class RedactionPolicy(BaseModel):
    """The (default-ON) redaction configuration. Every field has a safe default, so
    constructing ``RedactionPolicy()`` already strips passwords, the secret headers,
    and the generic token shapes. ``pii_patterns`` is the consumer's domain data —
    extra regexes (e.g. a customer-id format) — never a magic constant baked into Zu.
    """

    model_config = {"frozen": True}

    # The structural floor — on by default; turning these off is an explicit choice.
    redact_credential_fields: bool = True
    drop_secret_headers: bool = True
    redact_token_shapes: bool = True
    # Consumer-configurable PII: extra regex sources applied to every string. The
    # canonical example is an email or phone format the consumer wants gone.
    pii_patterns: tuple[str, ...] = Field(default_factory=tuple)

    def _compiled_pii(self) -> tuple[re.Pattern[str], ...]:
        return tuple(re.compile(p) for p in self.pii_patterns)


# A payment-card-number SHAPE: 13–19 digits, optionally grouped by spaces/dashes. Luhn-gated
# at substitution time so a real PAN is redacted but a random long id (an order/variant id)
# is left alone.
_PAN_CANDIDATE: re.Pattern[str] = re.compile(r"\b(?:\d[ \-]?){13,19}\b")


def _luhn_ok(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = ord(ch) - 48
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _redact_pans(text: str) -> str:
    """Redact Luhn-valid payment card numbers anywhere in a string (a card pasted into a
    'why' note, a url, a free-text field). The card FIELD itself is also blanked wholesale
    by its credential-target name; this is the belt-and-suspenders pass."""
    def repl(m: re.Match[str]) -> str:
        digits = "".join(c for c in m.group(0) if c.isdigit())
        return REDACTED if 13 <= len(digits) <= 19 and _luhn_ok(digits) else m.group(0)
    return _PAN_CANDIDATE.sub(repl, text)


def _redact_string(text: str, policy: RedactionPolicy, pii: Iterable[re.Pattern[str]]) -> str:
    """Sweep one string for token shapes + payment-card numbers + consumer PII (pure)."""
    out = text
    if policy.redact_token_shapes:
        for pat in _TOKEN_PATTERNS:
            out = pat.sub(_token_replacement, out)
        out = _redact_pans(out)  # payment card numbers (Luhn-gated)
    for pat in pii:
        out = pat.sub(REDACTED, out)
    return out


def _token_replacement(m: re.Match[str]) -> str:
    """Keep an auditable shape where one exists: a ``key=secret`` parameter keeps
    its key (``key=[REDACTED]``); a URL userinfo keeps the scheme; everything else
    collapses to the bare placeholder."""
    g = m.group(0)
    if "=" in g and m.lastindex:
        return f"{m.group(1)}={REDACTED}"
    if g.lower().startswith(("http://", "https://")) or "://" in g:
        return f"{m.group(1)}{REDACTED}@"
    return REDACTED


def _is_credential_field(name: str) -> bool:
    low = name.lower()
    return any(h in low for h in _CREDENTIAL_FIELD_HINTS)


def _redact_value(value: object, policy: RedactionPolicy, pii: tuple[re.Pattern[str], ...],
                  *, key: str | None = None) -> object:
    """Recursively redact a JSON-ish value. ``key`` is the field name under which the
    value sits (so a credential-named field is blanked wholesale)."""
    if policy.redact_credential_fields and key is not None and _is_credential_field(key):
        return REDACTED if value is not None else None
    if isinstance(value, str):
        return _redact_string(value, policy, pii)
    if isinstance(value, dict):
        return _redact_mapping(value, policy, pii)
    if isinstance(value, (list, tuple)):
        return [_redact_value(v, policy, pii) for v in value]
    return value


def _redact_mapping(d: dict, policy: RedactionPolicy,
                    pii: tuple[re.Pattern[str], ...]) -> dict:
    """Redact a dict: drop secret headers wholesale, blank credential fields, and
    sweep every remaining string. A ``headers`` sub-dict is treated specially so a
    secret header's value never survives even partially."""
    out: dict = {}
    for k, v in d.items():
        if policy.drop_secret_headers and isinstance(k, str) and k.lower() == "headers" \
                and isinstance(v, dict):
            out[k] = _redact_headers(v, policy, pii)
            continue
        out[k] = _redact_value(v, policy, pii, key=k if isinstance(k, str) else None)
    return out


def _redact_headers(headers: dict, policy: RedactionPolicy,
                    pii: tuple[re.Pattern[str], ...]) -> dict:
    out: dict = {}
    for k, v in headers.items():
        if isinstance(k, str) and k.lower() in _SECRET_HEADERS:
            out[k] = REDACTED
        else:
            out[k] = _redact_value(v, policy, pii, key=k if isinstance(k, str) else None)
    return out


def redact_text(text: str, policy: RedactionPolicy | None = None) -> str:
    """Redact a single free-text string — the entry point the "why" intent affordance
    uses, so the human's narration is scrubbed exactly like a captured value."""
    policy = policy or RedactionPolicy()
    return _redact_string(text, policy, policy._compiled_pii())


def redact_payload(payload: dict, policy: RedactionPolicy | None = None) -> dict:
    """Redact an event payload (pure). The recorder calls this on EVERY shadow event
    BEFORE handing it to the sink, so a secret is gone before the event is hashed."""
    policy = policy or RedactionPolicy()
    return _redact_mapping(payload, policy, policy._compiled_pii())


def redact_event(event: object, policy: RedactionPolicy | None = None) -> object:
    """Return a copy of ``event`` with its ``payload`` redacted. Works on a pydantic
    Event (``model_copy(update=...)``) or a plain dict — whichever the caller holds —
    so the recorder can redact before append regardless of the event representation."""
    policy = policy or RedactionPolicy()
    if isinstance(event, dict):
        out = dict(event)
        if isinstance(out.get("payload"), dict):
            out["payload"] = redact_payload(out["payload"], policy)
        return out
    payload = getattr(event, "payload", None)
    if isinstance(payload, dict) and hasattr(event, "model_copy"):
        return event.model_copy(update={"payload": redact_payload(payload, policy)})
    return event
