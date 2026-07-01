"""The one-way projection from zu-tools' ``Surface`` onto the core ``SurfaceView``.

This is the ONLY coupling between the Action Surface (zu-tools) and the Pattern
layer (zu-patterns), and it goes THROUGH the core type — so zu-patterns never
imports zu-tools, and zu-core stays pydantic-only. ``Surface`` is the rich
producer shape (it carries the harness-side ``handle_map``); ``SurfaceView`` is
the modality-agnostic currency the recognizer reads. The projection drops
``handle_map`` (never recognizer-visible — it mirrors ``action_surface._emit``,
which excludes it from the log) and folds each ``Affordance`` into a frozen
``SurfaceAffordance``.

A future screenshot/lidar/CSV reducer is another producer of ``SurfaceView``;
this adapter is the web one.
"""

from __future__ import annotations

from zu_core.surface import SurfaceAffordance, SurfaceView

from .action_surface import Surface


def to_surface_view(s: Surface) -> SurfaceView:
    """Project a zu-tools ``Surface`` onto the core ``SurfaceView`` (pure)."""
    return SurfaceView(
        title=s.title,
        url=s.url,
        affordances=tuple(
            SurfaceAffordance(
                handle=a.handle,
                role=a.role,
                label=a.label,
                value=a.value,
                states=tuple(a.states),
                group=a.group,
                enclosing_label=a.enclosing_label,
            )
            for a in s.affordances
        ),
        context=tuple(s.context),
        blind=s.blind,
        blind_reason=s.blind_reason,
    )
