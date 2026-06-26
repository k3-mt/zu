"""Shared escalation types — the currency of escalate→diagnose→repair→de-escalate.

When the cheap deterministic executor gets STUCK (no resolvable handle, or an act
that fired but changed nothing), it reads the diagnostic slice of ``content_view``
and asks a repairer what to do. The repairer answers with a :class:`Repair`. These
two value objects — :class:`Repair` and :class:`ProblemContext` — are the shared
currency of that control flow (Issue #41 §2.5, §5).

They live in **zu_core** on purpose: both executors speak them — ``zu-shadow``'s
``execute()`` and ``zu-patterns``'s ``mpc_run`` — and ``zu-patterns`` must NEVER
import ``zu-shadow`` (a cycle: ``zu-shadow`` depends on ``zu-core`` AND ``zu-cli``).
Putting the shared types here lets both speak them with no cross-package import.
The ``Repairer`` Protocol + the model-backed ``DefaultRepairer`` live in
``zu-shadow`` (a clean test double); ``mpc_run`` takes a plain async callable of
the same shape (Issue #41 §2.5 DECISION, §9.9).

``Step`` is deliberately NOT a field here — it lives in ``zu-shadow``, which
``zu-core`` cannot import. A producer that has a step carries it alongside; this
context carries the step's INDEX (the resume cursor) plus the two views and the
reason, which is all the shared seam needs.

Frozen dataclasses (stdlib only) — no model, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass

from .content_view import ContentView
from .surface import SurfaceView


@dataclass(frozen=True)
class Repair:
    """A repairer's answer — what to do about a stuck step.

    ``'fill'`` is a REVERSIBLE repair (fill the one missing field, then retry);
    ``'human'`` escalates to a person (the commit-boundary fallback); ``'abort'``
    gives up. Only a reversible ``'fill'`` is ever auto-applied — a payment /
    committing / redacted target is forced to ``'human'`` by the repairer
    (Issue #41 §5, §6 risk).
    """

    kind: str  # 'fill' | 'human' | 'abort'
    handle: str | None = None
    value: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class ProblemContext:
    """Everything a repairer needs to diagnose one stuck step.

    ``view`` is ALREADY the ``WANT_DIAGNOSTIC`` slice (errors + field_states) —
    the only content the escalation reads. ``index`` is the step's position (the
    resume cursor); ``surface`` is the content-free action view; ``reason`` is
    the stuck signal (``'unresolved'`` | ``'no_op'``) (Issue #41 §2.5, §5).
    """

    index: int
    surface: SurfaceView
    view: ContentView
    reason: str  # 'unresolved' | 'no_op'
