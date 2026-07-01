"""SEMANTIC-TARGET capture — name an action by WHAT it acts on, not WHERE.

Every captured user action identifies its target by ``{role, name, label}`` — the
same accessibility-grounded currency the core ``surface`` types speak (§4 handles /
§5 ``SurfaceView``). NEVER a CSS selector, an XPath, or a pixel coordinate: those
are brittle (a redesign breaks them) and untransferable (they cannot feed the §4
locator / §5 recognizer). A semantic target re-resolves on a changed page, which is
the whole reason a synthesized agent can be *resilient* rather than pixel-frozen.

``SemanticTarget`` is a thin, frozen value object that reuses ``role``/``label``
exactly as :class:`zu_core.surface.SurfaceAffordance` does, plus the accessible
``name`` (the click target's accessible name). The capture helpers turn a raw
abstract-stream event into a redaction-ready ``data.shadow.*`` payload.
"""

from __future__ import annotations

from pydantic import BaseModel

from zu_core import events as ev
from zu_core.surface import SurfaceAffordance

# STRUCTURAL credential signals — locale-INDEPENDENT, the PRIMARY basis for marking a
# field a credential. An <input type=password> and the autocomplete tokens a browser
# uses for secret fields (payment card number/security code, and password fields). These
# fire regardless of the label's language, so a French "Prüfziffer"/"code de sécurité"
# CVV carrying autocomplete=cc-csc is still caught.
_CREDENTIAL_INPUT_TYPES: frozenset[str] = frozenset({"password"})
_CREDENTIAL_AUTOCOMPLETE: frozenset[str] = frozenset({
    "cc-number", "cc-csc", "cc-exp", "cc-exp-month", "cc-exp-year", "cc-name",
    "current-password", "new-password", "one-time-code",
})
# The English phrase list is kept ONLY as a documented, weak SECONDARY fallback for a
# field whose structure the harness could not resolve (no input type / autocomplete
# threaded through). It is locale-specific and so silently misses on a non-English site
# — never rely on it as the sole signal.
_CREDENTIAL_TARGET_HINTS: tuple[str, ...] = ("password", "passwd", "secret", "token",
                                             "api key", "api_key", "apikey", "otp",
                                             "cvv", "cvc", "pin", "security code",
                                             # payment-card secrets — the agent must NEVER hold
                                             # these; a real payment goes through the §8 broker.
                                             "card number", "cardnumber", "card no",
                                             "credit card", "debit card", "expiration", "expiry",
                                             "iban", "sort code", "account number")


def _is_cc_autocomplete(token: str | None) -> bool:
    """A payment-card autocomplete token (cc-number/cc-csc/cc-exp/…) — the commit/credential
    guards treat any of these as a payment-card control regardless of label language."""
    return bool(token) and token.lower().startswith("cc-")  # type: ignore[union-attr]


class SemanticTarget(BaseModel):
    """A user-action target, identified the way the core surface currency does:
    ``role`` (a free string, e.g. ``button``/``link``/``textbox``), the accessible
    ``name``, and a human ``label``. NO selector, NO coordinates — re-resolvable on
    a changed page. Frozen so it is a stable value on the log.

    ``input_type`` / ``autocomplete`` / ``submits`` are the locale-independent
    STRUCTURAL signals threaded from the harness/CDP layer (mirroring the fields on
    :class:`zu_core.surface.SurfaceAffordance`): the raw ``<input type>``, the
    autocomplete token, and whether the control submits/commits. The credential and
    commit guards drive off these first; the English phrase lists are only a fallback."""

    model_config = {"frozen": True}

    role: str
    name: str = ""
    label: str = ""
    input_type: str | None = None
    autocomplete: str | None = None
    submits: bool = False

    @classmethod
    def from_affordance(cls, a: SurfaceAffordance, *, name: str = "") -> SemanticTarget:
        """Build a target from a core ``SurfaceAffordance`` — the bridge from a §5
        SurfaceView the live recorder reduced to a recorded action target. The
        affordance's ``label`` carries through; ``name`` is the accessible name the
        CDP locate step resolved (the affordance has no separate name field). The
        structural signals (``input_type``/``autocomplete``/``submits``) carry through
        too, so a downstream credential/commit guard sees them."""
        return cls(role=a.role, name=name or a.label, label=a.label,
                   input_type=a.input_type, autocomplete=a.autocomplete, submits=a.submits)

    def to_payload(self) -> dict:
        out: dict = {"role": self.role, "name": self.name, "label": self.label}
        # Only emit structural signals when present, so a recording made without them
        # (or an older fixture) is byte-for-byte unchanged.
        if self.input_type is not None:
            out["input_type"] = self.input_type
        if self.autocomplete is not None:
            out["autocomplete"] = self.autocomplete
        if self.submits:
            out["submits"] = True
        return out


def capture_click(target: SemanticTarget, *, intent: str | None = None) -> tuple[str, dict]:
    """A ``data.shadow.user.click`` (type, payload). ``intent`` is the OPTIONAL,
    reviewed "why" narration — carried but NEVER auto-promoted into the agent."""
    payload: dict = {"target": target.to_payload()}
    if intent is not None:
        payload["intent"] = intent
    return ev.SHADOW_USER_CLICK, payload


def _is_credential_target(target: SemanticTarget) -> bool:
    """Whether a type target is a CREDENTIAL input — so its value is recorded under a
    credential-named key the redaction stage blanks wholesale.

    PRIMARY signal is STRUCTURAL and locale-independent: an ``<input type=password>``
    or a payment/secret autocomplete token (``cc-number``/``cc-csc``/``current-password``
    …). These fire regardless of label language. The English phrase list is only a
    documented SECONDARY fallback for a field whose structure was not threaded through."""
    it = (target.input_type or "").lower()
    if it in _CREDENTIAL_INPUT_TYPES:
        return True
    ac = (target.autocomplete or "").lower()
    if ac in _CREDENTIAL_AUTOCOMPLETE or _is_cc_autocomplete(ac):
        return True
    # Fallback ONLY: the English phrase list, which silently misses on a non-English site.
    blob = f"{target.role} {target.name} {target.label}".lower()
    return any(h in blob for h in _CREDENTIAL_TARGET_HINTS)


def capture_type(target: SemanticTarget, value: str, *,
                 intent: str | None = None) -> tuple[str, dict]:
    """A ``data.shadow.user.type`` (type, payload). The recorder MARKS a credential
    target: a password/secret field's value goes under a ``password`` key that the
    redaction stage (run before append) blanks wholesale, so a credential is never
    recorded verbatim. A non-credential value rides under ``value`` and is still
    swept for token shapes by redaction. Capture marks; redaction enforces the floor."""
    payload: dict = {"target": target.to_payload()}
    if _is_credential_target(target):
        payload["password"] = value  # credential-named ⇒ redaction blanks it wholesale
    else:
        payload["value"] = value
    if intent is not None:
        payload["intent"] = intent
    return ev.SHADOW_USER_TYPE, payload


def capture_navigate(url: str, *, intent: str | None = None) -> tuple[str, dict]:
    """A ``data.shadow.user.navigate`` (type, payload). The URL is redaction-swept
    (credentials/tokens in the query stripped) before it reaches the log."""
    payload: dict = {"url": url}
    if intent is not None:
        payload["intent"] = intent
    return ev.SHADOW_USER_NAVIGATE, payload


def capture_page_loaded(url: str, title: str) -> tuple[str, dict]:
    """A ``data.shadow.page.loaded`` (type, payload) — a settled page; the locus a
    subsequent action's semantic target re-resolves against."""
    return ev.SHADOW_PAGE_LOADED, {"url": url, "title": title}


def capture_network_response(url: str, status: int, host: str) -> tuple[str, dict]:
    """A ``data.shadow.network.response`` (type, payload) — METADATA only (no body,
    no headers beyond the host). The synthesized agent's egress allowlist is induced
    from the ``host`` values across these events."""
    return ev.SHADOW_NETWORK_RESPONSE, {"url": url, "status": status, "host": host}


def capture_scroll(direction: str, y: int = 0) -> tuple[str, dict]:
    """A ``data.shadow.user.scroll`` (type, payload) — a settled scroll up/down. Context,
    not an action step: it records that the human had to scroll to reach the next thing."""
    d = direction if direction in ("up", "down") else "down"
    return ev.SHADOW_USER_SCROLL, {"direction": d, "y": int(y)}
