"""ZU-RAIL-9 — a recognized pattern's prediction is VERIFIED by a rail Monitor; a
behaviour mismatch fires a detector. The pattern is a prior, NEVER ground truth.

The guarantee is TWO-SIDED and modelled as a liveness-by-deadline property:

  * SUCCESS criterion = a POSTCONDITION / EVENTUALLY property — the predicted
    success surface is, by definition, ABSENT until the interaction completes, so
    it must NOT fire on the pre-interaction surface. It is satisfied the instant
    the success state appears; it VIOLATES only at the deadline (a terminal event)
    if the state never appeared.
  * FAILURE criterion = a SAFETY property — a known failure CONTEXT appearing
    (e.g. an error alert) fires immediately; the pre-interaction state (no error)
    satisfies it.

So: a SUCCEEDING run (success state appears post-interaction, no failure context)
fires NOTHING — including across the pre-interaction surfaces — and a FAILING run
(success state never appears by the deadline, or a failure context appears) fires
VIOLATION → escalation. That two-sided behaviour IS the guarantee.

All offline, $0: hand-built SurfaceViews + synthetic event logs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from zu_core import events as ev
from zu_core.invariants import compile_spec
from zu_core.ports import MonitorState, RunContext
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_patterns.login_form import LoginForm


def _ev(etype: str, payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type=etype, payload=payload)


def _ctx(events: list) -> RunContext:
    # The compiled Monitor only reads ctx.events; a SimpleNamespace is enough.
    return cast(RunContext, SimpleNamespace(events=events))


def _login_view() -> SurfaceView:
    return SurfaceView(
        affordances=(
            SurfaceAffordance(handle="a1", role="textbox", label="Email"),
            SurfaceAffordance(handle="a2", role="textbox", label="Password", states=("password",)),
            SurfaceAffordance(handle="a3", role="button", label="Sign in"),
        ),
    )


# A terminal event marks "the interaction/run is complete" — the deadline by which
# a success postcondition must have held at least once.
_DEADLINE = _ev(ev.TASK_TERMINAL, {"reason": "done"})


def test_success_invariant_compiles_to_a_monitor() -> None:
    pat = LoginForm()
    result = pat.recognize(_login_view())
    assert result is not None
    monitors = compile_spec(pat.success_invariants(result))
    assert monitors
    # each compiled object satisfies the Monitor shape (name + evaluate).
    for mon in monitors:
        assert isinstance(mon.name, str) and mon.name.startswith("pattern.login_form.")
        assert callable(mon.evaluate)


def test_pattern_mismatch_fires_detector() -> None:
    """The named ZU-RAIL-9 proof (the MISMATCH direction).

    Recognize a login form → compile its success Invariant → feed an event log
    where the predicted post-surface (an account/logout affordance) NEVER appears
    and the interaction COMPLETES (a terminal/deadline event) → assert the compiled
    Monitor returns VIOLATION. A wrong prior is caught as a detector firing, never
    silently obeyed.
    """
    pat = LoginForm()
    result = pat.recognize(_login_view())
    assert result is not None
    monitor = compile_spec(pat.success_invariants(result))[0]

    # The behaviour MISMATCH: after the (proposed) submit, the surface still shows
    # only a "Sign in" button — the predicted "Logout/Account" affordance is absent
    # — and the run reaches its deadline without it ever appearing.
    mismatch_log = [
        _ev(ev.PATTERN_RECOGNIZED, {"archetype": "login_form", "confidence": 0.95}),
        _ev(ev.SURFACE_CAPTURED, {"affordances": [{"label": "Sign in"}], "handles": ["a3"]}),
        _DEADLINE,
    ]
    verdict = monitor.evaluate(_ctx(mismatch_log))
    assert verdict is not None
    assert verdict.state is MonitorState.VIOLATION

    # Crucially, BEFORE the deadline the same un-satisfied stream is INERT — the
    # liveness property is not yet falsifiable, so it must NOT prematurely fire.
    pre_deadline = mismatch_log[:-1]
    assert monitor.evaluate(_ctx(pre_deadline)) is None


def test_pattern_match_does_not_fire() -> None:
    """The named ZU-RAIL-9 proof (the MATCH direction) — the second side.

    The SAME pattern + a SUCCEEDING run: the pre-interaction login surface (which
    by definition lacks the account/logout affordance), then the post-interaction
    surface that DOES show it, then the deadline. Across the WHOLE stream — pre-
    interaction surfaces included — the success Monitor must return OK (no
    VIOLATION). The prior was confirmed by observation; the rail stays silent.

    This test FAILS against the old THROUGHOUT compilation (which violated on the
    pre-interaction surface lacking the success affordance) and PASSES under the
    EVENTUALLY-by-deadline semantics.
    """
    pat = LoginForm()
    result = pat.recognize(_login_view())
    assert result is not None
    monitor = compile_spec(pat.success_invariants(result))[0]

    success_log = [
        _ev(ev.PATTERN_RECOGNIZED, {"archetype": "login_form", "confidence": 0.95}),
        # PRE-interaction: the login surface — success affordance ABSENT. Under
        # THROUGHOUT this fold alone would VIOLATE; under EVENTUALLY it is inert.
        _ev(ev.SURFACE_CAPTURED, {"affordances": [{"label": "Sign in"}], "handles": ["a3"]}),
        # POST-interaction: the success affordance appears.
        _ev(ev.SURFACE_CAPTURED, {"affordances": [{"label": "Logout"}], "handles": ["a9"]}),
        _DEADLINE,
    ]

    # No VIOLATION at ANY prefix of the stream (pre-interaction surfaces included).
    for i in range(1, len(success_log) + 1):
        assert monitor.evaluate(_ctx(success_log[:i])) is None, f"fired at prefix len {i}"


def test_failure_context_fires_immediately_and_is_inert_pre_interaction() -> None:
    """The FAILURE criterion is a correct SAFETY property.

    A known failure CONTEXT (an error alert) firing the instant it appears; the
    pre-interaction surface (no error) is INERT. This is the THROUGHOUT-negated
    shape ("throughout: NOT contains(error)"), NOT a positive must-contain.
    """
    pat = LoginForm()
    result = pat.recognize(_login_view())
    assert result is not None
    fail_monitors = compile_spec(pat.failure_invariants(result))
    assert fail_monitors
    assert all(m.name.startswith("pattern.login_form.") for m in fail_monitors)
    monitor = fail_monitors[0]

    # A normal pre-interaction surface with NO error context — inert (a positive
    # must-contain-THROUGHOUT would WRONGLY fire here; the negated safety shape
    # does not).
    clean = [_ev(ev.SURFACE_CAPTURED, {"affordances": [{"label": "Sign in"}], "handles": ["a3"]})]
    assert monitor.evaluate(_ctx(clean)) is None

    # The failure context appears → fire immediately (no deadline needed).
    errored = clean + [
        _ev(ev.SURFACE_CAPTURED, {"affordances": [{"label": "error"}], "labels": ["error"]}),
    ]
    verdict = monitor.evaluate(_ctx(errored))
    assert verdict is not None
    assert verdict.state is MonitorState.VIOLATION


def test_failure_invariant_is_also_a_monitor() -> None:
    pat = LoginForm()
    result = pat.recognize(_login_view())
    assert result is not None
    fail_monitors = compile_spec(pat.failure_invariants(result))
    assert fail_monitors
    assert all(m.name.startswith("pattern.login_form.") for m in fail_monitors)
