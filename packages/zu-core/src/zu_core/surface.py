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

from pydantic import BaseModel, ConfigDict


class SurfaceAffordance(BaseModel):
    """One thing the policy can do, as the recognizer sees it.

    ``handle`` is opaque and stable (``a1``, ``a2`` …) — the same handle the
    Action Surface assigns. ``role`` is a free string (NOT an enum) so a new
    producer can introduce roles without a core edit. ``states`` is a tuple so
    the whole value object is hashable/frozen.
    """

    model_config = ConfigDict(frozen=True)

    handle: str
    role: str
    label: str = ""
    value: str | None = None
    states: tuple[str, ...] = ()


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
