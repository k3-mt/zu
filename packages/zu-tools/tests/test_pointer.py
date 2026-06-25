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
    """A fake browser session that, LIKE THE REAL CONTAINER, REQUIRES a {role,name}
    ``locator`` on a ``locate`` command — a bare handle it never sees. So a pointer
    that forwarded a model handle (or had no harness-side resolution) would fail to
    locate; only a harness-resolved locator succeeds. This is what makes the
    handle-only-with-no-resolution path observably wrong, not silently masked."""

    def __init__(self, bounds: list[float] | None, cursor=(0.0, 0.0)) -> None:
        self._bounds = bounds
        self._cursor = cursor
        self.sent: list[dict] = []

    async def send(self, cmd: dict) -> dict:
        self.sent.append(cmd)
        if cmd["op"] == "locate":
            locator = cmd.get("locator")
            if not isinstance(locator, dict) or not locator.get("role"):
                # The real container errors without a resolved locator (§5.2).
                return {"error": "locator required (a {role, name} dict)"}
            if self._bounds is None:
                return {"error": "no such element"}
            return {"bounds": self._bounds, "cursor": list(self._cursor)}
        if cmd["op"] == "pointer":
            return {"dispatched": len(cmd["samples"])}
        return {}

    async def close(self) -> None:  # pragma: no cover - not used here
        pass


class _Ctx:
    def __init__(self, task_id: str) -> None:
        self.spec = type("S", (), {"task_id": task_id})()


def _seed_handle_map(run_key: str, mapping: dict[str, dict]) -> None:
    """Stand in for action_surface(op=open): register a live entry for the run and
    populate its shared handle_map, the harness-side indirection the pointer resolves
    against. Done via the production registry helpers — no injected shared backend."""
    from zu_tools import _session

    with _session._LOCK:
        _session._RUNS[run_key] = _session._RunEntry(handle=object())
    _session.put_handle_map(run_key, mapping)


async def test_tool_move_click_resolves_generates_and_dispatches() -> None:
    session = _FakeSession(bounds=[400, 300, 80, 24], cursor=(10, 10))
    ctx = _Ctx("run-mc")
    _seed_handle_map("run-mc", {"a3": {"role": "button", "name": "Place order"}})
    tool = PointerControl(session=session, seed="fixed-seed")
    out = await tool(ctx, op="move_click", handle="a3")
    assert out["pointer"]["clicked"] is True
    assert out["pointer"]["handle"] == "a3"
    assert out["pointer"]["samples"] > 0
    # the handle was resolved HARNESS-SIDE to {role,name} before locate (the model
    # never supplied a selector); the container saw the resolved locator.
    locate = next(c for c in session.sent if c["op"] == "locate")
    assert locate["locator"] == {"role": "button", "name": "Place order"}
    assert "handle" not in locate  # the opaque handle never reaches the container
    # the dispatch carried a mousemove stream and asked for a click
    dispatch = next(c for c in session.sent if c["op"] == "pointer")
    assert dispatch["click"] is True
    assert len(dispatch["samples"]) == out["pointer"]["samples"]


async def test_tool_handle_not_in_map_is_stale_not_a_model_fallback() -> None:
    # A handle with NO entry in the shared map: a stale_handle escalation, never a
    # crash and never a model-supplied locator forwarded to the container.
    session = _FakeSession(bounds=[400, 300, 80, 24])
    ctx = _Ctx("run-stale")
    _seed_handle_map("run-stale", {"a1": {"role": "button", "name": "OK"}})
    out = await PointerControl(session=session)(ctx, op="move_click", handle="a99")
    assert out["stale_handle"] == "a99" and "error" in out
    # it never even sent a locate — there was nothing to resolve to
    assert not any(c["op"] == "locate" for c in session.sent)


async def test_tool_stale_handle_escalates_not_crashes() -> None:
    # The handle resolves, but the container can no longer find the element (the page
    # changed): still an escalation, not a crash.
    session = _FakeSession(bounds=None)  # locate fails for a resolved locator
    ctx = _Ctx("run-gone")
    _seed_handle_map("run-gone", {"a3": {"role": "button", "name": "Gone"}})
    out = await PointerControl(session=session)(ctx, op="move_click", handle="a3")
    assert out["stale_handle"] == "a3"
    assert "error" in out


async def test_tool_requires_open_session() -> None:
    # No session open for the run AND no injected session.
    out = await PointerControl()(_Ctx("run-none"), handle="a1")
    assert "open browser session" in out["error"]


async def test_tool_seed_makes_dispatch_reproducible() -> None:
    s1 = _FakeSession(bounds=[400, 300, 80, 24], cursor=(10, 10))
    s2 = _FakeSession(bounds=[400, 300, 80, 24], cursor=(10, 10))
    _seed_handle_map("run-r1", {"a1": {"role": "button", "name": "B"}})
    _seed_handle_map("run-r2", {"a1": {"role": "button", "name": "B"}})
    o1 = await PointerControl(session=s1, seed="abc")(_Ctx("run-r1"), handle="a1")
    o2 = await PointerControl(session=s2, seed="abc")(_Ctx("run-r2"), handle="a1")
    assert o1["samples"] == o2["samples"]


# --- THE PRODUCTION-PATH cross-tool sharing test (§4/§5) -----------------
#
# This is the test that prevents the masking the adversarial review found. It
# constructs ActionSurface and PointerControl THE WAY THE LOOP DOES — each with its
# OWN backend (no shared injected backend, no session injected into BOTH tools). The
# ONLY thing they share is the run id (ctx.spec.task_id), through which the
# module-level run registry lets the pointer ATTACH to the session action_surface
# opened, and RESOLVE the handle action_surface mapped — all harness-side.


class _LiveLikeSession:
    """One fake session standing in for the shared live container: it answers axtree
    (so action_surface can reduce), and locate/pointer (so the pointer can act) — and,
    like the real container, REQUIRES a {role,name} locator on locate."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.closed = False

    async def send(self, cmd: dict) -> dict:
        self.sent.append(cmd)
        op = cmd["op"]
        if op == "axtree":
            return {"axtree": [{"role": {"value": "button"},
                                "name": {"value": "Place order"}, "ignored": False}],
                    "title": "Shop", "url": cmd.get("url", "")}
        if op == "locate":
            loc = cmd.get("locator")
            if not isinstance(loc, dict) or not loc.get("role"):
                return {"error": "locator required"}
            return {"bounds": [400, 300, 80, 24], "cursor": [0.0, 0.0]}
        if op == "pointer":
            return {"dispatched": len(cmd["samples"]), "clicked": bool(cmd.get("click"))}
        return {}

    async def close(self) -> None:
        self.closed = True


class _PerToolBackend:
    """A per-tool backend (each tool builds its OWN in production). Implements the
    run-scoped lease keyed in its OWN _sessions — which is exactly why the BACKEND
    cannot be the cross-tool sharing point: the pointer's backend instance is a
    DIFFERENT object with an EMPTY _sessions. Sharing must come from the module
    registry, not from here."""

    def __init__(self, session: _LiveLikeSession) -> None:
        self._session = session
        self._sessions: dict = {}
        self.opened = 0

    async def open_session(self, spec: dict) -> _LiveLikeSession:
        self.opened += 1
        return self._session

    async def open_run_session(self, spec: dict, *, run_key: str) -> _LiveLikeSession:
        self.opened += 1
        self._sessions[run_key] = self._session
        return self._session


async def test_action_surface_open_then_pointer_attaches_same_run_no_shared_backend() -> None:
    from zu_tools._session import close_run
    from zu_tools.action_surface import ActionSurface

    live = _LiveLikeSession()
    ctx = _Ctx("run-prod")

    # action_surface builds its OWN backend (we inject a per-tool fake to avoid Docker)
    # and opens the run's session; it stores the live handle + handle_map in the
    # SHARED module registry.
    surface = ActionSurface(backend=_PerToolBackend(live), allow_private=True)
    out = await surface(ctx, op="open", url="http://shop.test/")
    assert [a["label"] for a in out["action_surface"]["affordances"]] == ["Place order"]
    assert "handle_map" not in out  # (c): never in the model-visible obs

    # (a) the pointer in the SAME run, given NO backend and NO session, ATTACHES to
    # the same live session via the module registry and succeeds.
    pointer = PointerControl(seed="s")
    pout = await pointer(ctx, op="move_click", handle="a1")
    assert pout["pointer"]["clicked"] is True
    assert pout["pointer"]["handle"] == "a1"
    # (b) the {role,name} came from the SHARED handle_map action_surface populated —
    # the model supplied only the handle. The container saw the resolved locator.
    locate = next(c for c in live.sent if c["op"] == "locate")
    assert locate["locator"] == {"role": "button", "name": "Place order"}
    # both the axtree (surface) and the pointer dispatch hit the SAME live session.
    assert any(c["op"] == "axtree" for c in live.sent)
    assert any(c["op"] == "pointer" for c in live.sent)

    # (d) after the run completes, run-end teardown closes the shared session and
    # drops the registry entry — no leak.
    from zu_tools import _session
    await close_run("run-prod")
    assert live.closed is True
    assert "run-prod" not in _session._RUNS
