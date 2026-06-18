"""empty — fires when an observation carried no usable content."""

from __future__ import annotations

from zu_core.ports import RunContext, Scope, Severity, Verdict

from . import _html_of


class EmptyDetector:
    name = "empty"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        html = _html_of(ctx)
        if len(html.strip()) == 0:
            return Verdict(severity=Severity.ESCALATE, detector=self.name, detail="empty observation")
        return None
