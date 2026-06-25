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

# Target role/name/label tokens that mark an input as a CREDENTIAL field, so the
# recorder records its typed value under a credential-named key the redaction stage
# blanks wholesale — a password is never recorded verbatim, even pre-redaction-sweep.
_CREDENTIAL_TARGET_HINTS: tuple[str, ...] = ("password", "passwd", "secret", "token",
                                             "api key", "api_key", "apikey", "otp",
                                             "cvv", "cvc", "pin", "security code",
                                             # payment-card secrets — the agent must NEVER hold
                                             # these; a real payment goes through the §8 broker.
                                             "card number", "cardnumber", "card no",
                                             "credit card", "debit card", "expiration", "expiry",
                                             "iban", "sort code", "account number")


class SemanticTarget(BaseModel):
    """A user-action target, identified the way the core surface currency does:
    ``role`` (a free string, e.g. ``button``/``link``/``textbox``), the accessible
    ``name``, and a human ``label``. NO selector, NO coordinates — re-resolvable on
    a changed page. Frozen so it is a stable value on the log."""

    model_config = {"frozen": True}

    role: str
    name: str = ""
    label: str = ""

    @classmethod
    def from_affordance(cls, a: SurfaceAffordance, *, name: str = "") -> SemanticTarget:
        """Build a target from a core ``SurfaceAffordance`` — the bridge from a §5
        SurfaceView the live recorder reduced to a recorded action target. The
        affordance's ``label`` carries through; ``name`` is the accessible name the
        CDP locate step resolved (the affordance has no separate name field)."""
        return cls(role=a.role, name=name or a.label, label=a.label)

    def to_payload(self) -> dict:
        return {"role": self.role, "name": self.name, "label": self.label}


def capture_click(target: SemanticTarget, *, intent: str | None = None) -> tuple[str, dict]:
    """A ``data.shadow.user.click`` (type, payload). ``intent`` is the OPTIONAL,
    reviewed "why" narration — carried but NEVER auto-promoted into the agent."""
    payload: dict = {"target": target.to_payload()}
    if intent is not None:
        payload["intent"] = intent
    return ev.SHADOW_USER_CLICK, payload


def _is_credential_target(target: SemanticTarget) -> bool:
    """A type target whose role/name/label marks it as a credential input — so its
    value is recorded under a credential-named key the redaction stage blanks."""
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
