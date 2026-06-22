"""HuggingFace models in the detector and validator roles (§8.5, §9.1).

The port is the role, assigned per agent. A zero-shot or text-classification
model that *gates control flow* is a **detector**; one that *checks the final
result* is a **validator**. A trained classifier as a detector is cheaper,
faster, and more reliable than asking an LLM the same yes/no question — the
right-sized-model discipline the economics rest on (§9.1).

These are configured per agent (a model + the labels that matter + a threshold),
so they enter the registry *by reference in config* rather than as a zero-config
entry point. Both reuse the same :class:`HfClient` seam as the tools.
"""

from __future__ import annotations

from zu_core.contracts import Result
from zu_core.ports import RunContext, Scope, Severity, Verdict

from .client import HfClient

_CONTENT_KEYS = ("html", "text", "content")


def _text_of(obs: object) -> str:
    """The text of an observation, concatenating the content keys (mirrors the
    built-in detectors so they agree on "the content")."""
    if isinstance(obs, dict):
        parts = [v for k in _CONTENT_KEYS if isinstance(v := obs.get(k), str) and v]
        return "\n".join(parts)
    return ""


class HfClassifierDetector:
    """Escalate (or stop) when a HuggingFace classifier flags the observation.

    Configure with a model and the labels that should trip control flow. With
    ``candidate_labels`` set it runs zero-shot; without, it runs the model's own
    text-classification head. The verdict severity is configurable (default
    ESCALATE) — the deterministic gate, decided by the classifier, never the
    policy.
    """

    scope = Scope.PER_OBSERVATION

    def __init__(
        self,
        client: HfClient,
        model: str,
        *,
        escalate_on: list[str],
        candidate_labels: list[str] | None = None,
        threshold: float = 0.5,
        severity: Severity = Severity.ESCALATE,
        name: str = "hf-classifier",
    ) -> None:
        self._client = client
        self._model = model
        self._escalate_on = {lbl.lower() for lbl in escalate_on}
        self._candidate_labels = candidate_labels
        self._threshold = threshold
        self._severity = severity
        self.name = name

    def inspect(self, ctx: RunContext) -> Verdict | None:
        text = _text_of(getattr(ctx, "observation", None))
        if not text.strip():
            return None
        if self._candidate_labels is not None:
            scored = self._client.zero_shot(text, self._candidate_labels, self._model)
        else:
            scored = self._client.text_classification(text, self._model)
        if not scored:
            return None
        top = scored[0]
        if top["label"].lower() in self._escalate_on and top["score"] >= self._threshold:
            return Verdict(
                severity=self._severity,
                detector=self.name,
                detail=f"{top['label']} ({top['score']:.2f})",
            )
        return None


class HfClassifierValidator:
    """Fail a result on finalise when a HuggingFace classifier flags its value.

    The result's text is classified; if the top label is one of ``fail_on`` over
    threshold, the validator returns a (default RETRY) verdict — e.g. a toxicity
    or refusal classifier checking the answer before it ships.
    """

    def __init__(
        self,
        client: HfClient,
        model: str,
        *,
        fail_on: list[str],
        candidate_labels: list[str] | None = None,
        threshold: float = 0.5,
        severity: Severity = Severity.RETRY,
        value_key: str | None = None,
        name: str = "hf-classifier-check",
    ) -> None:
        self._client = client
        self._model = model
        self._fail_on = {lbl.lower() for lbl in fail_on}
        self._candidate_labels = candidate_labels
        self._threshold = threshold
        self._severity = severity
        self._value_key = value_key
        self.name = name

    def _result_text(self, result: Result) -> str:
        if not isinstance(result.value, dict):
            return ""
        if self._value_key is not None:
            v = result.value.get(self._value_key)
            return v if isinstance(v, str) else ""
        # join the string leaves of the value
        return "\n".join(str(v) for v in result.value.values() if isinstance(v, str))

    def check(self, result: Result, ctx: RunContext) -> Verdict | None:
        text = self._result_text(result)
        if not text.strip():
            return None
        if self._candidate_labels is not None:
            scored = self._client.zero_shot(text, self._candidate_labels, self._model)
        else:
            scored = self._client.text_classification(text, self._model)
        if not scored:
            return None
        top = scored[0]
        if top["label"].lower() in self._fail_on and top["score"] >= self._threshold:
            return Verdict(
                severity=self._severity,
                detector=self.name,
                detail=f"{top['label']} ({top['score']:.2f})",
            )
        return None
