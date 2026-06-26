"""The one-way projection from zu-tools' ``Surface`` (+ HTML) onto the core
``ContentView``.

The sibling of :func:`zu_tools.surface_adapter.to_surface_view`. Where that
adapter projects the action view, this one projects the reading view: it takes
the rich producer ``Surface`` (which already carries the captured affordances and
their AX states) plus the page ``html`` and produces the CORE
:class:`zu_core.content_view.ContentView` via :func:`reduce_content`. This goes
THROUGH the core type, so zu-patterns/zu-shadow speak the projection without
importing zu-tools, and zu-core NEVER imports this (Issue #41 §2.4).

The ``Surface`` affordances are re-expressed as :class:`AxNode` records so the
SAME diagnostic reducer (field_states from AX states, alerts from alert roles)
runs whether the caller has a raw accessibility tree or an already-reduced
``Surface``.
"""

from __future__ import annotations

from zu_core.content_view import ContentView

from .action_surface import AxNode, Surface
from .content_surface import reduce_content


def _affordances_as_nodes(s: Surface) -> list[AxNode]:
    """Re-express the surface's affordances as accessibility-tree nodes so the
    diagnostic reducer reads their role/label/value/states uniformly. The
    affordance label becomes the node ``name`` (its human-meaningful label)."""
    return [
        AxNode(
            role=a.role,
            name=a.label,
            value=a.value,
            states=list(a.states),
        )
        for a in s.affordances
    ]


def to_content_view(s: Surface, html: str = "") -> ContentView:
    """Project a zu-tools ``Surface`` (+ HTML) onto the core ``ContentView`` (pure,
    one-way). Mirrors :func:`to_surface_view`; zu_core never imports this."""
    return reduce_content(_affordances_as_nodes(s), html, url=s.url, title=s.title)
