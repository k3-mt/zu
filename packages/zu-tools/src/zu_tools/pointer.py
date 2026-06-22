"""pointer — synthesise human-like cursor movement (Engineering Design §12).

Given the cursor's current position and a target resolved from a handle (§11.3),
this generates a movement *path* and dispatches it as real input events. Genuine
movement is required because hover-menus, sliders, drag-and-drop, and canvas
apps respond to the pointer event *stream* — the sequence of moves — not to a
single click at the destination.

Two halves, cleanly split:

* :func:`pointer_path` — a **deterministic, seeded** generator that computes the
  whole trajectory *before* the cursor moves. The seed (the run id or a
  configured value) makes the path reproducible: a re-run regenerates the same
  trajectory, so it is fully testable offline at $0. It composes destination
  detection, a cubic-Bézier curve, velocity-from-distance easing, bounded
  perpendicular jitter, velocity noise, variable timing, last-mile
  micro-corrections, and a Fitts's-law duration (MT = a + b·log₂(2D/W)).

* :class:`PointerControl` — the tool. In the live arm it resolves a handle's
  locator to on-screen bounds via the browser session, runs ``pointer_path``
  harness-side (so the path is recorded and reproducible), then streams the
  ``mousemove`` samples and the ``mousedown``/``mouseup`` over CDP, where
  ``Input.dispatchMouseEvent`` produces ``isTrusted = true`` events
  indistinguishable from a physical mouse. A stale handle is an escalation, not
  a crash (§11.3).

No nondeterministic clock or global RNG is used — the generator draws only from
a seeded :class:`random.Random`, so the same (start, target, seed) always yields
the same samples.
"""

from __future__ import annotations

import math
import random
from typing import Any

from pydantic import BaseModel, Field

from zu_core.ports import CAP_NET, CAP_SANDBOX, EGRESS_OPEN, BrowserSessionHandle, SessionBackend

# Fitts's-law constants (seconds). MT = a + b·log₂(2D/W). The defaults give
# plausible sub-second moves for typical web targets; both are tunable per run.
FITTS_A = 0.08
FITTS_B = 0.045

# Base interval between mousemove samples (ms) before per-sample timing noise.
_BASE_INTERVAL_MS = 16.0


class Target(BaseModel):
    """An on-screen target: its bounding box ``[x, y, w, h]`` in CSS pixels."""

    bounds: list[float] = Field(..., min_length=4, max_length=4)

    @property
    def width_for_fitts(self) -> float:
        # Fitts's W is the target extent along the approach; the smaller dimension
        # is the conservative (harder) choice and clamps to >= 1px.
        return max(1.0, min(self.bounds[2], self.bounds[3]))


class MoveSample(BaseModel):
    """One dispatched cursor position. ``dt`` is the ms gap since the previous
    sample (variable timing); ``t`` is the cumulative ms from the start."""

    x: float
    y: float
    dt: float
    t: float


def _rng(seed: int | str) -> random.Random:
    # random.Random seeded with a str/int is stable across processes (version-2
    # seeding, independent of PYTHONHASHSEED) — what makes a run reproducible.
    return random.Random(seed)


def _pick_point(bounds: list[float], rng: random.Random) -> tuple[float, float]:
    """Destination detection (§12.2): a point within the target — its centre
    nudged by a small seeded offset, so repeated clicks don't land on one pixel."""
    x, y, w, h = bounds
    cx, cy = x + w / 2.0, y + h / 2.0
    # stay within the inner 60% so the point is comfortably inside the element
    cx += rng.uniform(-0.3, 0.3) * w
    cy += rng.uniform(-0.3, 0.3) * h
    return cx, cy


def _fitts_time(distance: float, width: float, a: float, b: float) -> float:
    """MT = a + b·log₂(2D/W), clamped to a small floor for tiny moves."""
    ratio = max(1.0, 2.0 * distance / width)
    return max(0.05, a + b * math.log2(ratio))


def _ease_in_out(u: float) -> float:
    """easeInOutQuad — accelerate from rest, cruise, decelerate into the target
    (velocity as a function of distance, §12.2)."""
    if u < 0.5:
        return 2.0 * u * u
    return 1.0 - ((-2.0 * u + 2.0) ** 2) / 2.0


def _bezier(p0: tuple[float, float], p1: tuple[float, float],
            p2: tuple[float, float], p3: tuple[float, float], t: float) -> tuple[float, float]:
    """A point on the cubic Bézier at parameter ``t`` (path curvature, §12.2/§12.3)."""
    mt = 1.0 - t
    a, b, c, d = mt ** 3, 3 * mt * mt * t, 3 * mt * t * t, t ** 3
    x = a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0]
    y = a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]
    return x, y


def _control_points(
    start: tuple[float, float], dest: tuple[float, float], rng: random.Random
) -> tuple[tuple[float, float], tuple[float, float]]:
    """Two control points that bow the path into a gentle arc — perpendicular to
    the start→dest line, offset by a seeded fraction of the distance."""
    sx, sy = start
    dx, dy = dest
    vx, vy = dx - sx, dy - sy
    dist = math.hypot(vx, vy) or 1.0
    # unit perpendicular
    px, py = -vy / dist, vx / dist
    bow = rng.uniform(-0.18, 0.18) * dist
    c1 = (sx + vx * 0.33 + px * bow, sy + vy * 0.33 + py * bow)
    c2 = (sx + vx * 0.66 + px * bow * 0.6, sy + vy * 0.66 + py * bow * 0.6)
    return c1, c2


def pointer_path(
    start: tuple[float, float],
    target: Target,
    seed: int | str,
    *,
    fitts_a: float = FITTS_A,
    fitts_b: float = FITTS_B,
) -> list[MoveSample]:
    """Compute the whole trajectory from ``start`` to a point in ``target``.

    Deterministic in ``(start, target.bounds, seed)``: same inputs → same path.
    The returned samples are mousemove positions with variable inter-sample
    timing; the caller brackets the final position with a press/release.
    """
    rng = _rng(seed)
    dest = _pick_point(target.bounds, rng)
    distance = math.hypot(dest[0] - start[0], dest[1] - start[1])
    total_s = _fitts_time(distance, target.width_for_fitts, fitts_a, fitts_b)

    # Sample count scales with duration; floor keeps short moves smooth.
    n = max(8, int((total_s * 1000.0) / _BASE_INTERVAL_MS))
    c1, c2 = _control_points(start, dest, rng)

    # Jitter scales gently with distance, capped so it never overwhelms the path.
    jitter_amp = min(2.5, 0.01 * distance)

    samples: list[MoveSample] = []
    t_cum = 0.0
    for i in range(1, n + 1):
        u = i / n
        eased = _ease_in_out(u)
        bx, by = _bezier(start, c1, c2, dest, eased)
        # bounded perpendicular jitter + velocity noise, both seeded
        bx += rng.uniform(-jitter_amp, jitter_amp)
        by += rng.uniform(-jitter_amp, jitter_amp)
        dt = _BASE_INTERVAL_MS * (1.0 + rng.uniform(-0.4, 0.6))  # variable timing
        t_cum += dt
        samples.append(MoveSample(x=bx, y=by, dt=dt, t=t_cum))

    samples += _micro_corrections(dest, t_cum, rng)
    return samples


def _micro_corrections(
    dest: tuple[float, float], t_cum: float, rng: random.Random
) -> list[MoveSample]:
    """Last-mile overshoot-and-correct, then a settling jitter (§12.2/§12.3)."""
    out: list[MoveSample] = []
    dx, dy = dest
    # a small overshoot past the target...
    over = (dx + rng.uniform(-3, 3), dy + rng.uniform(-3, 3))
    dt = _BASE_INTERVAL_MS * (1.0 + rng.uniform(-0.2, 0.4))
    t_cum += dt
    out.append(MoveSample(x=over[0], y=over[1], dt=dt, t=t_cum))
    # ...then settle exactly onto the destination.
    dt = _BASE_INTERVAL_MS * (1.0 + rng.uniform(-0.2, 0.4))
    t_cum += dt
    out.append(MoveSample(x=dx, y=dy, dt=dt, t=t_cum))
    return out


def _seed_from_ctx(ctx: Any, configured: int | str | None) -> int | str:
    """The path seed: a configured value, else the run's task id, else a constant
    (still deterministic — the path is reproducible either way)."""
    if configured is not None:
        return configured
    spec = getattr(ctx, "spec", None)
    task_id = getattr(spec, "task_id", None)
    return str(task_id) if task_id is not None else "zu-pointer"


class PointerControl:
    """Tier-3 tool: move the cursor to a handle and click it, like a person.

    ``op=move_click`` (default): resolve ``handle`` to on-screen bounds via the
    browser session, generate a seeded human-like path to it, dispatch the
    mousemove stream + press/release over CDP (``isTrusted`` events), and record
    the path on the event log. A stale/unknown handle returns ``stale_handle``
    (an escalation signal), never a crash.

    The path generator is pure and lives in :func:`pointer_path`; this class is
    the I/O around it.
    """

    name = "pointer"
    tier = 3  # pairs with the Action Surface — both speak role+name handles
    schema = {
        "name": "pointer",
        "description": (
            "Move the cursor to an action-surface handle and click it with genuine, "
            "human-like movement (curved path, variable speed, micro-corrections) so "
            "hover-menus, sliders and canvas widgets respond. Pass the handle from "
            "action_surface; the path is generated and dispatched for you. If "
            "stale_handle comes back, re-capture the surface and retry."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "op": {"type": "string", "enum": ["move_click", "move"]},
                "handle": {"type": "string", "description": "an action_surface handle (a1, a2 …)"},
                "locator": {"type": "object", "description": "an explicit {role, name} locator"},
            },
            "required": ["handle"],
        },
    }
    prompt_fragment = (
        "pointer(handle): move the cursor to an action_surface handle along a human-like "
        "path and click it (real isTrusted events). Use it when a plain click won't trigger "
        "hover/drag/canvas behaviour."
    )
    capabilities = frozenset({CAP_NET, CAP_SANDBOX})
    egress = frozenset({EGRESS_OPEN})

    def __init__(
        self,
        session: BrowserSessionHandle | None = None,
        backend: SessionBackend | None = None,
        *,
        seed: int | str | None = None,
        fitts_a: float = FITTS_A,
        fitts_b: float = FITTS_B,
    ) -> None:
        # A pointer acts on an ALREADY-OPEN page, so it shares a session opened by
        # action_surface/browser. ``session`` is that live handle; ``backend`` is
        # an escape hatch for opening one standalone (rare).
        self._session = session
        self._backend = backend
        self._seed = seed
        self._fitts_a = fitts_a
        self._fitts_b = fitts_b

    async def __call__(
        self,
        ctx: Any,
        op: str = "move_click",
        handle: str | None = None,
        locator: dict | None = None,
    ) -> dict:
        if op not in ("move_click", "move"):
            return {"error": f"unknown op {op!r}; use move_click/move"}
        if not handle and not locator:
            return {"error": "pointer requires a handle (or an explicit locator)"}
        if self._session is None:
            return {"error": "pointer needs an open browser session (open one with "
                              "action_surface(op=open) or browser(op=open) first)"}

        # Resolve the handle/locator to on-screen bounds + current cursor position.
        located = await self._session.send(
            {"op": "locate", "handle": handle, "locator": locator}
        )
        if not isinstance(located, dict) or located.get("bounds") is None:
            reason = located.get("error") if isinstance(located, dict) else "bad response"
            # A handle that no longer resolves is an escalation, not a failure.
            return {"stale_handle": handle, "error": f"could not locate target: {reason}"}

        start = tuple(located.get("cursor", [0.0, 0.0]))[:2]
        target = Target(bounds=[float(v) for v in located["bounds"]])
        seed = _seed_from_ctx(ctx, self._seed)
        samples = pointer_path(
            (float(start[0]), float(start[1])), target, seed,
            fitts_a=self._fitts_a, fitts_b=self._fitts_b,
        )

        dispatch = {
            "op": "pointer",
            "samples": [s.model_dump() for s in samples],
            "click": op == "move_click",
        }
        resp = await self._session.send(dispatch)
        if isinstance(resp, dict) and resp.get("error"):
            return {"error": f"pointer dispatch failed: {resp['error']}"}

        dest = samples[-1]
        # The path lands on the event log via the observation — the audit story
        # for "where did the cursor go" (§12.2). Samples included for replay; the
        # summary keeps the common read cheap.
        return {
            "pointer": {
                "handle": handle,
                "clicked": op == "move_click",
                "samples": len(samples),
                "duration_ms": round(dest.t, 1),
                "dest": {"x": round(dest.x, 1), "y": round(dest.y, 1)},
                "seed": str(seed),
            },
            "samples": dispatch["samples"],
        }
