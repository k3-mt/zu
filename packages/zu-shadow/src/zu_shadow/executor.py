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

import hashlib
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable
from uuid import UUID, uuid4

from zu_core import events as ev
from zu_core.content_view import WANT_DIAGNOSTIC, ContentView, Want
from zu_core.contracts import Event
from zu_core.escalation import ProblemContext
from zu_core.ports import ModelProvider, ModelRequest
from zu_core.surface import SurfaceView

from .redaction import REDACTED, redact_payload

if TYPE_CHECKING:
    from zu_core.bus import EventBus

    from .escalate import Repairer

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
    via: str             # "exact" | "param" | "model" | "navigate" | "interstitial"
    #                      | "no_op" | "escalated" | "repaired" | "unresolved"
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
    Action Surface); ``act`` operates one by its opaque handle (never a selector).

    ``content_view`` is the SEPARATE second projection — the readable substance of
    the page (the diagnostic slice on escalation). The content-free path
    (``perceive``/``act``/``current_url``) never reads it, and content NEVER feeds
    the surface state id (Issue #41 §0, §5); it is read ONLY on escalation, only
    the requested ``want`` slice."""

    def perceive(self) -> SurfaceView: ...
    def act(self, handle: str, kind: str, value: str | None = None) -> None: ...
    def current_url(self) -> str: ...
    def content_view(self, want: frozenset[Want]) -> ContentView: ...  # NEW second projection


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


# A control that dismisses a blocking overlay (cookie/consent banner, popup) that the
# demonstration didn't include — generic verbs only, anchored so it matches a dismiss button,
# not "Accept terms" text. Accepting/closing a banner is reversible; it just unblocks the step.
_DISMISS = re.compile(r"(?i)^(accept( all)?( cookies)?|agree|i agree|allow( all)?|got it|"
                      r"ok(ay)?|continue|close|dismiss|no thanks|reject( all)?|"
                      r"accept all cookies)$")


def _interstitial(surface: SurfaceView) -> str | None:
    """A dismiss control for a cookie/consent/popup overlay blocking the step — so the run
    isn't derailed by an interstitial that wasn't in the recording."""
    for a in surface.affordances:
        if a.role in ("button", "link") and _DISMISS.match(_norm(a.label)):
            return a.handle
    return None


def _surface_digest(surface: SurfaceView) -> str:
    """A LOCAL "changed nothing" comparator: url + title + the sorted affordance set.

    The same shape the learned FSM keys on (``zu_patterns._surface_state``: url +
    title + handles), but with each affordance's role+label folded in too — so a
    page transition that REUSES a handle (``a1``) for a genuinely different control
    is still seen as a change. Replicated here in stdlib ``hashlib`` rather than
    imported — ``zu-shadow`` must NOT take a ``zu-patterns`` dependency (Issue #41 §5,
    group constraint). The comparator is built from the ACTION view only; page
    CONTENT never enters this digest, so it stays consistent with the content-free
    FSM and never fragments per error-text variant."""
    affs = sorted(f"{a.handle}\t{a.role}\t{a.label}" for a in surface.affordances)
    canon = "\n".join([surface.url, surface.title, *affs])
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _field_value(surface: SurfaceView, handle: str) -> str | None:
    """The current value of a field by handle, if the surface carries one — used
    to tell a real type (the field now holds text) from a no-op type (still empty)."""
    for a in surface.affordances:
        if a.handle == handle:
            return a.value
    return None


def _is_no_op(before: SurfaceView, after: SurfaceView, step: Step, handle: str) -> bool:
    """Did the act fire but change NOTHING? True when the local surface digest is
    unchanged AND — for a ``type`` step — the target field is still empty. Checked
    ONLY after the existing interstitial-dismiss / re-perceive retries have run, so a
    slow-loading page is not a false no-op (Issue #41 §5, §9 risk 7)."""
    if _surface_digest(before) != _surface_digest(after):
        return False
    if step.kind == "type":
        return not (_field_value(after, handle) or "")
    return True


def _resolve_exact(step: Step, surface: SurfaceView,
                   ov: dict[str, str]) -> tuple[str | None, str, str | None]:
    """Resolve a step WITHOUT the model: EXACT re-resolve (the demonstrated target by
    role+name) or PARAM (type an override into a field). The model-choice generalisation is
    the LAST resort, tried only after exact retries fail — so a lazy-loading or banner-blocked
    page is retried for the real control instead of the model grabbing a wrong one."""
    value = ov.get(_norm(step.name), step.value) if step.kind == "type" else None
    handle = _match(surface, step.role, step.name)
    if handle is not None:
        return handle, "exact", value
    if step.kind == "type":
        f = _first_field(surface)
        if f is not None:
            return f, "param", value
    return None, "", value


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
    max_retries: int = 2,
    repairer: Repairer | None = None,
    escalation_budget: int = 1,
    on_checkpoint: Callable[[int], Awaitable[None]] | None = None,
    bus: EventBus | None = None,
    trace_id: UUID | None = None,
    task_id: UUID | None = None,
) -> RunReport:
    """Drive the demonstrated path on the live ``session``, generalising via ``overrides``
    (a typed value keyed by the step's name, e.g. {"search": "collars"}) and the model for
    unmatched controls. When a target isn't found, dismiss a blocking interstitial (cookie /
    consent / popup) and RE-PERCEIVE before escalating — so a banner that wasn't in the
    recording, or content still loading, doesn't derail the run. Stops at the commit boundary.

    On a stuck step — no resolvable handle (``unresolved``), or an act that fired but changed
    NOTHING (``no_op``) — the bounded ``escalate→diagnose→repair→de-escalate`` loop runs (only
    when a ``repairer`` is injected): read the SMALL diagnostic slice of ``content_view``
    (``WANT_DIAGNOSTIC``, the ONLY content read), ask the repairer, and on a REVERSIBLE ``fill``
    apply it and RETRY the step on the content-free path. A ``human``/``abort`` answer, a
    committing step, or budget exhaustion sets ``escalated_at`` and returns. ``on_checkpoint(i)``
    fires on each SUCCESSFUL step so ``escalated_at`` maps 1:1 to a last-known-good cursor for
    resume (Issue #41 §5, §6)."""
    ov = {_norm(k): v for k, v in (overrides or {}).items()}
    report = RunReport()
    tid = trace_id or uuid4()
    kid = task_id or uuid4()

    async def _emit(type_: str, payload: dict) -> None:
        """Append an escalation/repair/content event to the hash-chained log — REDACTED
        first, and carrying provenance + hashes, NEVER body text (Issue #41 §4)."""
        if bus is None:
            return
        await bus.publish(Event(
            trace_id=tid, task_id=kid, type=type_,
            source="zu-shadow.executor", payload=redact_payload(payload),
        ))

    async def _capture_diagnostic(step: Step, i: int, surface: SurfaceView,
                                  reason: str) -> RunReport | None:
        """The bounded repair loop. Reads ``content_view(WANT_DIAGNOSTIC)`` (the only content
        read), emits the audit events, calls the repairer, and on a reversible ``fill`` applies
        it + re-perceives + retries step ``i`` on the content-free path (de-escalate). Returns
        the finished ``report`` if the run must stop (human/abort/committing/budget); ``None``
        if the step was repaired and the caller should re-resolve and continue."""
        if repairer is None:
            report.outcomes.append(StepOutcome(step, "unresolved", ok=False,
                                               detail="no resolvable target — escalate"))
            report.escalated_at = i
            return report
        escalations = 0
        while escalations < escalation_budget:
            diag = session.content_view(WANT_DIAGNOSTIC)  # the ONLY content read
            # CONTENT_CAPTURED carries url + region counts + per-unit hashes + view hash —
            # NEVER body text (the body never enters the hash chain).
            await _emit(ev.CONTENT_CAPTURED, {
                "url": diag.url, "want": sorted(w.value for w in WANT_DIAGNOSTIC),
                "view_hash": diag.hash(),
                "counts": {"errors": len(diag.errors), "field_states": len(diag.field_states)},
                "unit_hashes": ([u.content_hash for u in diag.errors]
                                + [f.content_hash for f in diag.field_states]),
            })
            await _emit(ev.STEP_ESCALATED, {"step": i, "reason": reason})
            # An (audit) marker that this step entered escalation — distinct from the
            # terminal 'escalated' outcome below, which also sets escalated_at.
            report.outcomes.append(StepOutcome(step, "escalated", ok=False, detail=reason))
            repair = await repairer.diagnose_and_repair(
                ProblemContext(index=i, surface=surface, view=diag, reason=reason),
                model, budget=escalation_budget - escalations)
            # NEVER auto-cross the commit boundary — a human/abort answer or a committing step
            # stops the run (only a REVERSIBLE fill is auto-applied).
            if repair.kind in ("human", "abort") or step.committing or repair.kind != "fill":
                report.outcomes.append(StepOutcome(step, "escalated", ok=False,
                                                   detail=repair.reason))
                report.escalated_at = i
                return report
            if repair.handle is None:
                report.outcomes.append(StepOutcome(step, "escalated", ok=False,
                                                   detail="repair named no resolvable field"))
                report.escalated_at = i
                return report
            # Apply the reversible fill. STEP_REPAIRED is redacted — the field label only,
            # NEVER the value typed (a fill is a new secret surface).
            session.act(repair.handle, "type", repair.value)
            await _emit(ev.STEP_REPAIRED, {"step": i, "repair_kind": "fill",
                                           "field": repair.reason})
            report.outcomes.append(StepOutcome(step, "repaired", handle=repair.handle,
                                               detail=repair.reason))
            escalations += 1
            # DE-ESCALATE: retry step i on the content-free path. The retry must clear the
            # SAME stuck signal — a fill that resolved a handle but still left a no-op (the
            # field stayed empty) is NOT progress; loop again (consuming budget) rather than
            # falsely completing.
            surface = session.perceive()
            handle, via, value = _resolve_exact(step, surface, ov)
            if handle is not None:
                before = surface
                session.act(handle, step.kind, value)
                surface = session.perceive()
                if not _is_no_op(before, surface, step, handle):
                    report.outcomes.append(StepOutcome(step, via, handle=handle, value=value))
                    return None  # back on the cheap path — caller continues to the next step
        # Budget exhausted — bounded, no infinite loop.
        report.outcomes.append(StepOutcome(step, "escalated", ok=False,
                                           detail="escalation budget exhausted"))
        report.escalated_at = i
        return report

    for i, step in enumerate(steps):
        if step.kind == "navigate":
            report.outcomes.append(StepOutcome(step, "navigate"))  # a consequence of the prior act
            if on_checkpoint is not None:
                await on_checkpoint(i)
            continue
        if step.committing and on_commit == "escalate":
            report.outcomes.append(StepOutcome(step, "escalated", ok=False,
                                               detail="commit boundary — route to a human / the broker"))
            report.escalated_at = i
            return report

        surface = session.perceive()
        handle, via, value = _resolve_exact(step, surface, ov)
        tries = 0
        while handle is None and tries < max_retries:
            inter = _interstitial(surface)
            if inter is not None:  # dismiss a cookie/consent/popup that wasn't demonstrated
                session.act(inter, "click", None)
                report.outcomes.append(StepOutcome(
                    Step(kind="click", role="button", name="(dismiss interstitial)"),
                    "interstitial", handle=inter))
            surface = session.perceive()  # re-perceive: the banner is gone / content settled
            handle, via, value = _resolve_exact(step, surface, ov)
            tries += 1

        if handle is None and step.kind == "click":  # GENERALISE only after exact retries fail
            handle, via = await _model_choose(step, surface, model), "model"

        if handle is None:
            # (a) UNRESOLVED — no resolvable handle. Route into the bounded repair loop.
            done = await _capture_diagnostic(step, i, surface, "unresolved")
            if done is not None:
                return done
            continue  # repaired + retried — on to the next step

        session.act(handle, step.kind, value)
        # (b) NO-OP — the act fired but changed nothing (only AFTER the interstitial/re-perceive
        # retries above, so a slow page is not a false positive). The re-perceive + compare runs
        # ONLY when a repairer is injected: without one there is nothing to do about a no-op, and
        # skipping the extra perceive keeps the cheap content-free path byte-for-byte unchanged.
        if repairer is not None:
            after = session.perceive()
            if _is_no_op(surface, after, step, handle):
                report.outcomes.append(StepOutcome(step, "no_op", handle=handle, value=value,
                                                   ok=False, detail="act fired but changed nothing"))
                done = await _capture_diagnostic(step, i, after, "no_op")
                if done is not None:
                    return done
                continue  # repaired + retried — on to the next step

        report.outcomes.append(StepOutcome(step, via, handle=handle, value=value))
        if on_checkpoint is not None:  # a SUCCESSFUL step → mark the resume cursor
            await on_checkpoint(i)

    report.completed = True
    return report
