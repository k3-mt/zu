"""empty — fires when a *fetched page* carried no usable content.

Scoped to page-content observations on purpose: it judges a fetch (a tool that
returned ``html``/``text``/``content``) and escalates when that content is empty
— the signal to climb to a browser. It must NOT fire on observations that are not
page fetches — e.g. ``html_parse`` returning ``{"matches": [...]}`` (a successful
extraction) or an error observation — or it would spuriously escalate after real
work. So: a content key present but blank -> escalate; no content key -> not our
concern (return None).
"""

from __future__ import annotations

from zu_core.ports import RunContext, Scope, Severity, Verdict

from . import _CONTENT_KEYS  # one source of truth for "what counts as page content"


class EmptyDetector:
    name = "empty"
    scope = Scope.PER_OBSERVATION

    def inspect(self, ctx: RunContext) -> Verdict | None:
        obs = getattr(ctx, "observation", None)
        if not isinstance(obs, dict):
            return None
        present = [k for k in _CONTENT_KEYS if k in obs]
        if not present:
            return None  # not a page-content observation — "empty" doesn't apply
        if all(not str(obs.get(k) or "").strip() for k in present):
            return Verdict(severity=Severity.ESCALATE, detector=self.name, detail="empty observation")
        return None
