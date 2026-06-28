"""Action-effect verification — the content-free silent-no-op oracle generalised UP
into zu-core (was a stand-in living downstream in conduit).

Two layers, both $0 (no live model, no network, no Docker):

* the pure primitives — ``SurfaceView.fingerprint`` (folds affordance states/values but
  NOT the per-render handle) and ``zu_core.effect.verify_effect`` (a four-signal,
  content-free before/after diff);
* the loop integration — a ``ScriptedProvider`` drives surface → click → surface against
  fake tools, and the loop emits ``data.effect.verified`` and flags a silent no-op back to
  the policy.
"""

from __future__ import annotations

from zu_core.bus import EventBus
from zu_core.contracts import Status, TaskSpec
from zu_core.effect import verify_effect
from zu_core.events import EFFECT_VERIFIED
from zu_core.loop import run_task
from zu_core.registry import Registry
from zu_core.surface import SurfaceAffordance, SurfaceView
from zu_providers.scripted import ScriptedProvider


def _surface(handle: str, *, states: tuple[str, ...] = (), label: str = "Opt",
             value: str | None = None, url: str = "u", title: str = "T") -> SurfaceView:
    return SurfaceView(
        title=title, url=url,
        affordances=(SurfaceAffordance(handle=handle, role="radio", label=label,
                                       value=value, states=states),),
    )


# --- the pure primitives -----------------------------------------------------------------


def test_fingerprint_ignores_handle_but_folds_states_and_values() -> None:
    # A re-render that renumbers the handle but changes nothing else must read as NO change:
    # the handle is excluded from the fingerprint.
    assert _surface("a1").fingerprint() == _surface("a2").fingerprint()
    # A state-only change (a radio became 'checked') MUST move the fingerprint — exactly the
    # change the coarse surface_state_id (url+title / sorted handles) cannot see.
    assert _surface("a1").fingerprint() != _surface("a1", states=("checked",)).fingerprint()
    # A value change moves it too.
    assert _surface("a1").fingerprint() != _surface("a1", value="x").fingerprint()


def test_verify_effect_change_vs_silent_no_op() -> None:
    before = _surface("a1")
    # acted control's own state flipped -> a real effect (None)
    assert verify_effect(before, _surface("a1", states=("checked",)), "a1") is None
    # a sibling/new label appeared -> a real effect
    after_label = SurfaceView(title="T", url="u", affordances=(
        SurfaceAffordance(handle="a1", role="radio", label="Opt"),
        SurfaceAffordance(handle="a2", role="button", label="Continue"),
    ))
    assert verify_effect(before, after_label, "a1") is None
    # handle renumbered, everything else identical -> SILENT NO-OP (the swatch didn't select)
    assert verify_effect(before, _surface("a2"), "a1") == "silent-no-op"


def test_verify_effect_finds_acted_control_by_identity_after_renumber() -> None:
    # The click re-rendered and renumbered the handle, but the acted control's state changed:
    # it is re-found by (role,label) identity, so the change is NOT missed.
    before = _surface("a1")
    after = _surface("a9", states=("checked",))
    assert verify_effect(before, after, "a1") is None


# --- the loop integration ----------------------------------------------------------------


class _FakeSurface:
    """A fake action_surface-shaped tool: returns a one-affordance surface whose handle/state
    the script controls, so before/after can be staged deterministically."""

    name = "surface"
    tier = 1
    schema = {"name": "surface", "parameters": {"type": "object", "properties": {
        "checked": {"type": "boolean"}, "renumber": {"type": "boolean"}}}}
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    async def __call__(self, ctx, checked: bool = False, renumber: bool = False) -> dict:
        handle = "a2" if renumber else "a1"
        states = ["checked"] if checked else []
        return {"action_surface": {"title": "T", "url": "u", "affordances": [
            {"handle": handle, "role": "radio", "label": "Opt", "value": None, "states": states}],
            "context": [], "blind": False, "blind_reason": None}, "surface_blind": False}


class _FakeClick:
    """A fake pointer-shaped tool: a click on a handle (arms effect verification)."""

    name = "click"
    tier = 1
    schema = {"name": "click", "parameters": {"type": "object",
                                              "properties": {"handle": {"type": "string"}}}}
    capabilities: frozenset[str] = frozenset()
    egress: frozenset[str] = frozenset()

    async def __call__(self, ctx, handle: str = "a1") -> dict:
        return {"pointer": {"handle": handle, "clicked": True, "samples": 1,
                            "duration_ms": 1.0, "dest": {"x": 0, "y": 0}, "seed": "s"}}


def _registry() -> Registry:
    reg = Registry()
    reg.register("tools", "surface", _FakeSurface())
    reg.register("tools", "click", _FakeClick())
    return reg


async def _run(moves: list[dict]) -> list:
    provider = ScriptedProvider.from_moves(moves)
    bus = EventBus()
    result = await run_task(TaskSpec(query="q"), provider, _registry(), bus)
    assert result.status is Status.SUCCESS
    return await bus.query()


async def test_loop_emits_effect_verified_changed() -> None:
    # surface(unchecked) -> click(a1) -> surface(checked): the acted control's state flipped,
    # so the effect is 'changed' and the before/after fingerprints differ.
    events = await _run([
        {"tool": "surface", "args": {"checked": False}},
        {"tool": "click", "args": {"handle": "a1"}},
        {"tool": "surface", "args": {"checked": True}},
        {"text": "{}", "finish": "stop"},
    ])
    verified = [e for e in events if e.type == EFFECT_VERIFIED]
    assert len(verified) == 1
    p = verified[0].payload
    assert p["acted_handle"] == "a1"
    assert p["result"] == "changed"
    assert p["before_fp"] != p["after_fp"]


async def test_loop_emits_silent_no_op_and_surfaces_signal() -> None:
    # surface -> click(a1) -> surface(renumbered, otherwise identical): the click changed
    # nothing, so the loop records a silent no-op AND flags it back to the policy.
    events = await _run([
        {"tool": "surface", "args": {"checked": False}},
        {"tool": "click", "args": {"handle": "a1"}},
        {"tool": "surface", "args": {"renumber": True}},
        {"text": "{}", "finish": "stop"},
    ])
    verified = [e for e in events if e.type == EFFECT_VERIFIED]
    assert len(verified) == 1
    p = verified[0].payload
    assert p["result"] == "silent-no-op"
    assert p["before_fp"] == p["after_fp"]  # handle renumber alone never moves the fingerprint
    # the silent no-op is surfaced to the policy on the after-surface tool.returned observation
    returned = [e for e in events if e.type == "harness.tool.returned"
                and e.payload.get("tool") == "surface"]
    assert returned[-1].payload["observation"].get("effect") == "silent-no-op"


async def test_no_act_between_surfaces_emits_no_effect_event() -> None:
    # Two surface captures with NO click between them: effect verification is inert (it only
    # fires for a click bracketed by surfaces) — no spurious no-op event.
    events = await _run([
        {"tool": "surface", "args": {"checked": False}},
        {"tool": "surface", "args": {"checked": True}},
        {"text": "{}", "finish": "stop"},
    ])
    assert [e for e in events if e.type == EFFECT_VERIFIED] == []
