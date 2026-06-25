"""The live executor — the agent USES a Shadow recording to do the task itself, and
GENERALISES it.

Record the task once (buy a muzzle); Shadow synthesises the path; this executor then
RE-RUNS it on the live site — and the next run can vary it (search "collars" instead of
"muzzles"). It is the §1.5 division made concrete: the recording bounds the action space
(the demonstrated procedure + semantic anchors), and where the live page diverges from
the demonstration the MODEL proposes within the bounded affordance set while the harness
disposes. Three resolution modes per step:

  * EXACT   — the demonstrated target still exists (a fixed-flow control like "Add to
              cart" / "Check out") → re-resolve it by role+name and act.
  * PARAM   — a typed value is overridden ("muzzles" → "collars"; the customer's own
              name/address) → type the override into the field.
  * MODEL   — the demonstrated specific target is GONE (you searched collars, so the
              muzzle product link isn't there) → the model picks the best handle from the
              CURRENT affordances (it emits a handle, never a selector), generalising.

The COMMIT BOUNDARY (a payment / place-order step) is never auto-crossed: the executor
escalates before it (a real payment is a §8 brokered capability, never the captured card).
The browser is an injected ``BrowserSession`` — a fake drives it at $0 in tests; the live
Playwright binding drives real Chrome.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from zu_core import events as ev
from zu_core.ports import ModelProvider, ModelRequest
from zu_core.surface import SurfaceView

from .redaction import REDACTED

_CLICKABLE = frozenset({"button", "link", "checkbox", "radio", "switch", "tab",
                        "menuitem", "option", "row", "gridcell"})
_FIELDS = frozenset({"textbox", "searchbox", "combobox"})
# Steps whose name names an irreversible money/commit action — never auto-crossed.
_COMMIT = re.compile(r"(?i)\b(place order|pay now|pay$|buy now|complete (order|purchase|"
                     r"payment)|confirm (and )?pay|submit order|checkout & pay)\b")
# A payment-card field — the agent must NEVER type a card; a real payment is a §8 brokered
# capability. A redacted secret value means the same: the agent doesn't hold the secret.
_PAYMENT_FIELD = re.compile(r"(?i)\b(card number|cardnumber|card no|credit card|debit card|"
                            r"expiration|expiry|cvv|cvc|security code|iban|sort code|"
                            r"account number)\b")


@dataclass(frozen=True)
class Step:
    """One step of the demonstrated path: what to do, on what (by role+name), the value
    typed, the human's why, and whether it crosses the commit boundary."""
    kind: str            # "click" | "type" | "navigate"
    role: str = ""
    name: str = ""
    value: str | None = None
    intent: str | None = None
    committing: bool = False


@dataclass
class StepOutcome:
    step: Step
    via: str             # "exact" | "param" | "model" | "navigate" | "escalated" | "unresolved"
    handle: str | None = None
    value: str | None = None
    ok: bool = True
    detail: str = ""


@dataclass
class RunReport:
    outcomes: list[StepOutcome] = field(default_factory=list)
    completed: bool = False
    escalated_at: int | None = None

    @property
    def acted(self) -> list[StepOutcome]:
        return [o for o in self.outcomes if o.handle is not None]


@runtime_checkable
class BrowserSession(Protocol):
    """The live browser the executor drives. The fake test double and the live Playwright
    binding both satisfy this. ``perceive`` returns the CURRENT page's affordances (the
    Action Surface); ``act`` operates one by its opaque handle (never a selector)."""

    def perceive(self) -> SurfaceView: ...
    def act(self, handle: str, kind: str, value: str | None = None) -> None: ...
    def current_url(self) -> str: ...


def _norm(s: str | None) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip().lower()


def steps_from_recording(events: list[Any]) -> list[Step]:
    """Turn a recording's events into the executable path: clicks/types/navigates, with the
    same cleanup the synthesizer applies (drop a focus-click before a type on the same
    target; collapse a consecutive duplicate), and the commit boundary marked."""
    raw: list[Step] = []
    for e in events:
        t = getattr(e, "type", "")
        p = getattr(e, "payload", {}) or {}
        if t == ev.SHADOW_USER_NAVIGATE:
            raw.append(Step(kind="navigate", value=p.get("url", "")))
            continue
        if t not in (ev.SHADOW_USER_CLICK, ev.SHADOW_USER_TYPE):
            continue
        tgt = p.get("target", {}) or {}
        kind = "click" if t == ev.SHADOW_USER_CLICK else "type"
        name = tgt.get("name") or tgt.get("label") or ""
        value = p.get("value")
        if value is None:
            value = p.get("password")  # a credential field's (redacted) value lives under this key
        committing = (
            (kind == "click" and bool(_COMMIT.search(name)))  # an irreversible order/pay click
            or value == REDACTED                              # a step needing a secret the agent lacks
            or bool(_PAYMENT_FIELD.search(name))              # a payment-card field — brokered (§8)
        )
        raw.append(Step(kind=kind, role=tgt.get("role", ""), name=name,
                        value=value, intent=p.get("intent"), committing=committing))
    # R2: drop a focus-click immediately followed by a type on the same target. R1: collapse
    # a consecutive duplicate. (The whys live on the events and are reviewed separately.)
    out: list[Step] = []
    for i, s in enumerate(raw):
        if s.kind == "click" and i + 1 < len(raw):
            nxt = raw[i + 1]
            if nxt.kind == "type" and _norm(nxt.name) == _norm(s.name):
                continue
        if out and out[-1].kind == s.kind and _norm(out[-1].name) == _norm(s.name) \
                and out[-1].value == s.value:
            continue
        out.append(s)
    return out


def _match(surface: SurfaceView, role: str, name: str) -> str | None:
    """Re-resolve the demonstrated target on the CURRENT page by role+name: an exact label
    match first, then a contained match (robust to small label drift)."""
    nm = _norm(name)
    if not nm:
        return None
    for a in surface.affordances:
        if _norm(a.label) == nm:
            return a.handle
    for a in surface.affordances:
        al = _norm(a.label)
        if al and (nm in al or al in nm) and (not role or a.role == role or
                                              (role in _FIELDS and a.role in _FIELDS)):
            return a.handle
    return None


def _first_field(surface: SurfaceView) -> str | None:
    for a in surface.affordances:
        if a.role in _FIELDS:
            return a.handle
    return None


async def _model_choose(step: Step, surface: SurfaceView, model: ModelProvider) -> str | None:
    """GENERALISE: the demonstrated control is gone, so the model picks the handle that best
    continues the task — bounded to the CURRENT affordances (it emits a handle, never a
    selector). A reply that names no real handle resolves to None → escalate, never guess."""
    clickable = [a for a in surface.affordances if a.role in _CLICKABLE]
    if not clickable:
        return None
    listing = "\n".join(f'{a.handle}: {a.role} "{a.label}"' for a in clickable)
    goal = step.intent or f"{step.kind} {step.name}".strip()
    req = ModelRequest(messages=[
        {"role": "system", "content": "You drive a web agent following a known task on a live "
         "site. The demonstrated control is not on this page. Pick the SINGLE affordance handle "
         "that best continues the task. Reply with ONLY the handle (e.g. a3)."},
        {"role": "user", "content": f"Step to continue: {goal}\nAffordances:\n{listing}\n\nHandle:"},
    ])
    resp = await model.complete(req)
    handles = {a.handle for a in clickable}
    for tok in re.findall(r"[A-Za-z]+\w*", resp.text or ""):
        if tok in handles:
            return tok
    return None


async def execute(
    steps: list[Step],
    session: BrowserSession,
    model: ModelProvider,
    *,
    overrides: dict[str, str] | None = None,
    on_commit: str = "escalate",
) -> RunReport:
    """Drive the demonstrated path on the live ``session``, generalising via ``overrides``
    (a typed value keyed by the step's name, e.g. {"search": "collars"}) and the model for
    unmatched controls. Stops and escalates at the first commit-boundary step."""
    ov = {_norm(k): v for k, v in (overrides or {}).items()}
    report = RunReport()
    for i, step in enumerate(steps):
        if step.kind == "navigate":
            report.outcomes.append(StepOutcome(step, "navigate"))  # a consequence of the prior act
            continue
        if step.committing and on_commit == "escalate":
            report.outcomes.append(StepOutcome(step, "escalated", ok=False,
                                               detail="commit boundary — route to a human / the broker"))
            report.escalated_at = i
            return report

        surface = session.perceive()
        value = ov.get(_norm(step.name), step.value) if step.kind == "type" else None

        handle = _match(surface, step.role, step.name)          # EXACT: the demonstrated target
        via = "exact"
        if handle is None and step.kind == "type":
            handle, via = _first_field(surface), "param"        # PARAM: type the override into a field
        if handle is None and step.kind == "click":
            handle, via = await _model_choose(step, surface, model), "model"  # MODEL: generalise

        if handle is None:
            report.outcomes.append(StepOutcome(step, "unresolved", ok=False,
                                               detail="no resolvable target — escalate"))
            report.escalated_at = i
            return report

        session.act(handle, step.kind, value)
        report.outcomes.append(StepOutcome(step, via, handle=handle, value=value))

    report.completed = True
    return report
