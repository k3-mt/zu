"""grounding — every extracted value must appear in retrieved content.

The anti-making-things-up check: a value the agent reports that is nowhere in
the content the run actually fetched fails grounding. It reads the run's
content from the event log via RunContext, so it proves provenance, not just
plausibility. Finalized against the event log in build step 6.
"""

from __future__ import annotations

from zu_core.contracts import Result
from zu_core.ports import RunContext, Severity, Verdict


def _retrieved_corpus(ctx: RunContext) -> str:
    """Concatenate everything the run fetched, from data.source.fetched events.

    Falls back to the current observation when the event log isn't populated
    yet (the loop wires the full log in build step 4).
    """
    chunks: list[str] = []
    for ev in getattr(ctx, "events", []) or []:
        payload = getattr(ev, "payload", {}) or {}
        for key in ("html", "text", "content"):
            if isinstance(payload.get(key), str):
                chunks.append(payload[key])
    obs = getattr(ctx, "observation", None)
    if isinstance(obs, dict):
        for key in ("html", "text", "content"):
            if isinstance(obs.get(key), str):
                chunks.append(obs[key])
    return "\n".join(chunks)


class GroundingValidator:
    name = "grounding"

    def check(self, result: Result, ctx: RunContext) -> Verdict | None:
        if not result.value:
            return None
        corpus = _retrieved_corpus(ctx)
        for field, value in result.value.items():
            if not isinstance(value, str) or not value.strip():
                continue
            if value not in corpus:
                return Verdict(
                    severity=Severity.RETRY,
                    detector=self.name,
                    detail=f"value for {field!r} not found in retrieved content",
                )
        return None
