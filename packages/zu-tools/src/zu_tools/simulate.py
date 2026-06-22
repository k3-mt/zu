"""simulate — world-model foresight as a Tool (Engineering Design §9.2, path 1).

A world model plugs into Zu three escalating ways; the simplest is "a tool the
policy calls for foresight before acting (``simulate(state, plan)``) — just
another registry entry." This is that tool: a generic primitive that hands the
policy a *prediction* of what a plan would do to a state, so the policy can
reason over the outcome before committing.

The discipline matters (the project's whole premise): the tool exposes the
primitive; the *model reasons*. ``simulate`` does not decide anything — it
returns a predicted outcome and lets the policy choose. The world model itself
is pluggable: an injected ``simulator`` callable (sync or async) wired per agent.
Unconfigured, the tool fails loudly with a clear message rather than fabricating
a prediction — a made-up rollout would be exactly the silent-success the runtime
forbids.
"""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from typing import Any

# A world model: current state + proposed plan → predicted next state/outcome.
Simulator = Callable[[dict, dict], "dict | Awaitable[dict]"]


class Simulate:
    name = "simulate"
    tier = 1  # foresight is a cheap call the policy makes before acting
    schema = {
        "name": "simulate",
        "description": (
            "Predict what a plan would do to a state BEFORE acting, using a world "
            "model. Pass the current state and the proposed plan; get back a "
            "predicted outcome to reason over. It does not act — it foresees."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "state": {"type": "object", "description": "the current state"},
                "plan": {"type": "object", "description": "the proposed plan/action to simulate"},
            },
            "required": ["state", "plan"],
        },
    }
    prompt_fragment = (
        "simulate(state, plan): roll a world model forward to predict the outcome of a "
        "plan before you commit to it; reason over the prediction, then act."
    )
    # Foresight is computation, not I/O — a local world model reaches nothing. A
    # hosted simulator would declare its egress on the injected simulator's behalf
    # via config; the default primitive declares least privilege.
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    def __init__(self, simulator: Simulator | None = None) -> None:
        self._simulator = simulator

    async def __call__(self, ctx: Any, state: dict, plan: dict) -> dict:
        if self._simulator is None:
            return {"error": "no world model configured for simulate; wire one via config "
                             "(zu_tools.simulate:Simulate with a simulator), then call again"}
        result = self._simulator(state, plan)
        if inspect.isawaitable(result):
            result = await result
        return {"prediction": result}
