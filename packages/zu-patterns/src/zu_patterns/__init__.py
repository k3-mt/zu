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
from .search import (
    Candidate,
    MpcDecision,
    MpcOutcome,
    Plan,
    PlanStep,
    fsm_from_events,
    fsm_from_shadow,
    fsm_from_shadow_events,
    live_mpc_step,
    merge_transition_models,
    mpc_run,
    plan,
)

__zu_spec__ = "§5"  # pattern recognition + guided search (issue #30: greppable spec anchor)

__all__ = [
    "Recognition",
    "recognize",
    "Commitment",
    "Signal",
    "ActionPrior",
    "DEFAULT_PRIORS",
    "classify_action",
    "Plan",
    "PlanStep",
    "fsm_from_events",
    "plan",
    "live_mpc_step",
    "mpc_run",
    "Candidate",
    "MpcDecision",
    "MpcOutcome",
    "fsm_from_shadow",
    "fsm_from_shadow_events",
    "merge_transition_models",
]
