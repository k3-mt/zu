"""zu-patterns — the policy-prior / move-ordering layer over the Action Surface.

The ``Pattern`` port itself lives in zu-core (``zu_core.ports.Pattern``); this
package ships the built-in patterns, the recognizer pass, the reversible-vs-
committing classifier, and the offline guided search over the Phase-1 FSM.
"""

from __future__ import annotations

from .recognizer import Recognition, recognize
from .reversibility import (
    DEFAULT_PRIORS,
    ActionPrior,
    Commitment,
    Signal,
    classify_action,
)
from .search import Plan, fsm_from_events, live_mpc_step, plan

__all__ = [
    "Recognition",
    "recognize",
    "Commitment",
    "Signal",
    "ActionPrior",
    "DEFAULT_PRIORS",
    "classify_action",
    "Plan",
    "fsm_from_events",
    "plan",
    "live_mpc_step",
]
