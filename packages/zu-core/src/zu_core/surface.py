"""The modality-agnostic surface view — the core currency the Pattern port speaks.

A "surface" is the set of things an agent can perceive and do at one step,
reduced from whatever raw modality produced it (a rendered DOM, a screenshot, a
LIDAR sweep, a CSV). zu-tools' ``action_surface`` is one *producer* of this
shape; a future vision/lidar/tabular reducer is another. Keeping the currency
here — a pure-pydantic type in zu-core — is what lets the Pattern port's
``recognize`` take a CORE type, never zu-tools' ``Surface`` (zu-core depends only
on pydantic and cannot import zu-tools). This is the §4.5 "keep the interface
modality-agnostic" intent made concrete.

The view deliberately OMITS the harness-side ``handle_map`` (handle → locator):
that indirection is never model/recognizer-visible (it mirrors
``action_surface._emit``, which excludes it from the event log). The recognizer
sees handles, roles, labels, states — never selectors.

These are frozen value objects: pydantic + stdlib only, no model, no I/O.
"""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ConfigDict


class SurfaceAffordance(BaseModel):
    """One thing the policy can do, as the recognizer sees it.

    ``handle`` is opaque and stable (``a1``, ``a2`` …) — the same handle the
    Action Surface assigns. ``role`` is a free string (NOT an enum) so a new
    producer can introduce roles without a core edit. ``states`` is a tuple so
    the whole value object is hashable/frozen.

    ``input_type`` / ``autocomplete`` / ``submits`` are locale-INDEPENDENT
    structural signals a producer may thread through from the harness/CDP layer
    (an ``<input type=password>``, an ``autocomplete=cc-number``/``cc-csc`` token,
    a ``button[type=submit]`` / form-submit control). Safety-critical guards drive
    off these — a credential field, a commit boundary — instead of matching an
    English word in the label, so a non-English site's ``Bezahlen`` submit button
    or ``Prüfziffer`` CVV field is still recognised. Empty/None when the producer
    did not (or could not) resolve them; all default so existing producers and
    fixtures are unaffected.
    """

    model_config = ConfigDict(frozen=True)

    handle: str
    role: str
    label: str = ""
    value: str | None = None
    states: tuple[str, ...] = ()
    input_type: str | None = None       # the raw <input type=…> (password/text/tel/…)
    autocomplete: str | None = None      # the autocomplete token (cc-number/cc-csc/current-password/…)
    submits: bool = False                # a submit/commit control (button[type=submit], form submit)
    # An opaque, content-free GROUP id shared by the options of ONE single-choice
    # group — a product-variant swatch/radio group (colour vs size), a tablist, a
    # listbox — derived from the enclosing accessibility group container, NEVER from
    # the option labels. It is what lets a consumer tell 'these three swatches are
    # colour, those three are size' and satisfy EACH group (a flat list cannot, #120);
    # ``None`` when the producer could not resolve a group. Deliberately OUT of
    # ``fingerprint`` — it is stable structure, not a selection effect — so existing
    # fixtures/digests are unaffected (it is purely additive).
    group: str | None = None
    # The accessible name of this control's ENCLOSING container — a card/list-item
    # heading, an ``aria-labelledby`` group name, the nearest section heading (#127).
    # A NAME-CLASS structural signal (the label OF a control's own group box), never
    # free page prose, so it is content-free and injection-safe. It disambiguates a
    # row of identically-named controls — a list of service cards whose buttons all
    # read 'Select', selectable only by their card heading. ``None`` when there is no
    # labelled enclosing container. Additive; out of ``fingerprint``.
    enclosing_label: str | None = None


class SurfaceView(BaseModel):
    """The reduced, modality-agnostic view of one step — the recognizer's input.

    ``url`` is a generic locus (``""`` for a non-web producer). ``context`` holds
    orienting, non-actionable text (headings, alerts, error text). ``blind``
    signals the reducer could not be trusted to be complete (the §11.4 escalation
    signal); a recognizer treats a blind surface as low-confidence territory.
    """

    model_config = ConfigDict(frozen=True)

    title: str = ""
    url: str = ""
    affordances: tuple[SurfaceAffordance, ...] = ()
    context: tuple[str, ...] = ()
    blind: bool = False
    blind_reason: str | None = None

    def fingerprint(self) -> str:
        """A stable, content-free digest of the surface's SHAPE — the before/after
        oracle action-effect verification compares (``zu_core.effect.verify_effect``).

        It folds ``title``/``url`` and, per affordance in document order,
        ``role``/``label``/``value``/``states`` — but DELIBERATELY NOT the opaque
        ``handle``: a click that re-renders a page often renumbers every handle while
        the surface is otherwise identical, and that must read as *no change*, not a
        spurious effect. Conversely a state-only change (a radio became ``checked``, a
        swatch became ``selected``) DOES move the fingerprint — which is exactly what
        the coarse ``surface_state_id`` (url+title, or sorted handles) cannot see, and
        why effect verification needs this finer digest. Content-free by construction:
        labels/roles/states are perception structure, never page prose (§9)."""
        parts = [f"t={self.title}", f"u={self.url}"]
        for a in self.affordances:
            states = ",".join(a.states)
            parts.append(f"r={a.role}\x1fl={a.label}\x1fv={a.value or ''}\x1fs={states}")
        basis = "\x1e".join(parts)
        return "sfp_" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
