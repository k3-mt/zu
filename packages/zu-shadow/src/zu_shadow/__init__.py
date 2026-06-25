"""zu-shadow — author a production agent by DEMONSTRATION (§2.8).

A Shadow recording *is* the event bus run over a HUMAN session: the human is the
policy for that one run, so recording costs almost nothing architecturally — the
recorder folds an abstract input/CDP stream into ``data.shadow.*`` events on the
same append-only log everything else uses. Four disciplines are load-bearing:

* **Redaction is DEFAULT-ON and runs BEFORE append** (``redaction``): secrets —
  passwords, ``Authorization``/``Cookie`` headers, tokens/API keys, configured PII
  — never reach :meth:`EventSink.append`. The "why" intent text is redacted too.
* **Capture is SEMANTIC** (``capture``): a user action is named by its target's
  ``{role, name, label}`` (the core ``surface`` currency, shared with §4 handles /
  §5 SurfaceView) — never a CSS selector or pixel coordinate.
* **The synthesizer is itself a Zu agent** (``synthesizer``): driven by a
  ``ModelProvider`` (offline-tested with ``ScriptedProvider``), it PROPOSES an
  agent spec + an induced ``Fsm`` + ``Invariant``s; the egress allowlist writes
  itself from the recorded ``network.response`` hosts.
* **Promotion is GATED by reproduced outcome** (``replay_gate``): a synthesized
  agent does not run on real data until it reproduces the recorded outcome, reusing
  zu-cli's ``offline.py``/``build.py``. The "why" resolutions are reviewed, never
  auto-promoted.
"""

from __future__ import annotations

from .capture import SemanticTarget, capture_click, capture_navigate, capture_type
from .recorder import RecordedSession, Recorder
from .redaction import RedactionPolicy, redact_event, redact_text
from .replay_gate import PromotionVerdict, verify_and_gate
from .synthesizer import SynthesisResult, Synthesizer

__all__ = [
    "PromotionVerdict",
    "RecordedSession",
    "Recorder",
    "RedactionPolicy",
    "SemanticTarget",
    "SynthesisResult",
    "Synthesizer",
    "capture_click",
    "capture_navigate",
    "capture_type",
    "redact_event",
    "redact_text",
    "verify_and_gate",
]
