"""pointer — human-like cursor movement (Engineering Design §12).

The generator is the value and is pure and seeded, so most of this proves its
properties deterministically with no browser: a fixed seed reproduces the path
exactly, the path lands inside the target, time is monotonic, the trajectory is
curved (not dead-straight), and Fitts's law makes a far/small target take longer
than a near/large one. The tool arm is exercised against a fake session that
resolves a handle to bounds and records the dispatch — and proves a stale handle
escalates rather than crashes.
"""

from __future__ import annotations

import math

from zu_tools.pointer import MoveSample, PointerControl, Target, pointer_path


def _line_distance(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    """Perpendicular distance from point p to the line a→b."""
    ax, ay = a
    bx, by = b
    px, py = p
    num = abs((by - ay) * px - (bx - ax) * py + bx * ay - by * ax)
    den = math.hypot(by - ay, bx - ax) or 1.0
    return num / den


def test_path_is_reproducible_for_a_seed() -> None:
    target = Target(bounds=[400, 300, 80, 24])
    a = pointer_path((0, 0), target, seed="run-123")
    b = pointer_path((0, 0), target, seed="run-123")
    assert a == b  # identical samples — a re-run regenerates the same trajectory


def test_different_seeds_differ() -> None:
    target = Target(bounds=[400, 300, 80, 24])
    a = pointer_path((0, 0), target, seed="run-1")
    b = pointer_path((0, 0), target, seed="run-2")
    assert a != b


def test_path_lands_inside_the_target() -> None:
    target = Target(bounds=[400, 300, 80, 24])
    samples = pointer_path((0, 0), target, seed=7)
    last = samples[-1]
    x, y, w, h = target.bounds
    assert x <= last.x <= x + w
    assert y <= last.y <= y + h


def test_time_is_monotonic_and_dt_positive() -> None:
    samples = pointer_path((0, 0), Target(bounds=[200, 200, 50, 50]), seed=1)
    times = [s.t for s in samples]
    assert times == sorted(times)
    assert all(s.dt > 0 for s in samples)


def test_path_is_curved_not_dead_straight() -> None:
    start, target = (0.0, 0.0), Target(bounds=[500, 0, 40, 40])
    samples = pointer_path(start, target, seed=42)
    dest = (samples[-1].x, samples[-1].y)
    # at least one mid-path sample is meaningfully off the straight start→dest line
    max_dev = max(_line_distance((s.x, s.y), start, dest) for s in samples[:-2])
    assert max_dev > 1.0


def test_fitts_law_far_small_takes_longer_than_near_large() -> None:
    near_large = pointer_path((0, 0), Target(bounds=[20, 0, 200, 200]), seed=3)
    far_small = pointer_path((0, 0), Target(bounds=[1500, 0, 8, 8]), seed=3)
    assert far_small[-1].t > near_large[-1].t
    assert len(far_small) > len(near_large)


def test_micro_corrections_present() -> None:
    # the final two samples are the overshoot + settle; the settle lands on dest
    samples = pointer_path((0, 0), Target(bounds=[300, 300, 60, 60]), seed=9)
    assert len(samples) >= 3
    # the penultimate (overshoot) is generally not exactly the final (settle)
    assert (samples[-1].x, samples[-1].y) != (samples[-2].x, samples[-2].y)


def test_movesample_is_serialisable() -> None:
    s = MoveSample(x=1.0, y=2.0, dt=16.0, t=16.0)
    assert s.model_dump() == {"x": 1.0, "y": 2.0, "dt": 16.0, "t": 16.0}


# --- the tool arm -------------------------------------------------------


class _FakeSession:
    def __init__(self, bounds: list[float] | None, cursor=(0.0, 0.0)) -> None:
        self._bounds = bounds
        self._cursor = cursor
        self.sent: list[dict] = []

    async def send(self, cmd: dict) -> dict:
        self.sent.append(cmd)
        if cmd["op"] == "locate":
            if self._bounds is None:
                return {"error": "no such element"}
            return {"bounds": self._bounds, "cursor": list(self._cursor)}
        if cmd["op"] == "pointer":
            return {"dispatched": len(cmd["samples"])}
        return {}

    async def close(self) -> None:  # pragma: no cover - not used here
        pass


async def test_tool_move_click_resolves_generates_and_dispatches() -> None:
    session = _FakeSession(bounds=[400, 300, 80, 24], cursor=(10, 10))
    tool = PointerControl(session=session, seed="fixed-seed")
    out = await tool(None, op="move_click", handle="a3")
    assert out["pointer"]["clicked"] is True
    assert out["pointer"]["handle"] == "a3"
    assert out["pointer"]["samples"] > 0
    # the dispatch carried a mousemove stream and asked for a click
    dispatch = next(c for c in session.sent if c["op"] == "pointer")
    assert dispatch["click"] is True
    assert len(dispatch["samples"]) == out["pointer"]["samples"]


async def test_tool_stale_handle_escalates_not_crashes() -> None:
    session = _FakeSession(bounds=None)  # locate fails
    tool = PointerControl(session=session)
    out = await tool(None, op="move_click", handle="a99")
    assert out["stale_handle"] == "a99"
    assert "error" in out


async def test_tool_requires_open_session() -> None:
    out = await PointerControl()(None, handle="a1")
    assert "open browser session" in out["error"]


async def test_tool_seed_makes_dispatch_reproducible() -> None:
    s1 = _FakeSession(bounds=[400, 300, 80, 24], cursor=(10, 10))
    s2 = _FakeSession(bounds=[400, 300, 80, 24], cursor=(10, 10))
    o1 = await PointerControl(session=s1, seed="abc")(None, handle="a1")
    o2 = await PointerControl(session=s2, seed="abc")(None, handle="a1")
    assert o1["samples"] == o2["samples"]
