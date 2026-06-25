"""vision_surface — the §4.4 VISION reducer (4K screenshot → action surface).

The §4.4 pattern applied to the pixel modality: heavy observation in (a
screenshot) → a model PROPOSES raw detections (the one irreducible step) → the
DETERMINISTIC reducer DISPOSES them into the SAME
:class:`~zu_tools.action_surface.Surface`/core ``SurfaceView`` the a11y Action
Surface produces. These tests drive it at $0 with a FAKE detector over a fake
Image — NO real vision model, NO network, NO Docker.

What is proven:
  * the reducer emits a Surface with opaque handles (model emits handles, never px);
  * it PRUNES the unusable on perceptibility grounds (tiny / off-screen / low
    confidence / occluded) — never by guessed task-relevance;
  * it does NOT reorder by task-relevance (handles follow detection order);
  * it sets BLIND when nothing is actionable (the last-tier escalate signal);
  * a handle RESOLVES to a click-point, and a stale handle ESCALATEs (no crash);
  * the tier-3 → tier-4 wiring lands on a REAL vision SurfaceView via VisionCapture;
  * the MODALITY-AGNOSTIC proof: zu_patterns.recognize matches an archetype over a
    VISION-produced SurfaceView identically to an a11y one.
"""

from __future__ import annotations

import base64

from zu_core.content import Image
from zu_core.surface import SurfaceView
from zu_tools.action_surface import Surface, reduce_surface
from zu_tools.surface_adapter import to_surface_view
from zu_tools.vision import VisionCapture
from zu_tools.vision_surface import (
    DetectedElement,
    VisionDetector,
    reduce_vision_surface,
)

# a 1x1 transparent PNG — the fake screenshot the fake detector "looks at".
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)
_IMG = Image(data=_PNG, mime="image/png")


# --- the reducer: the deterministic six steps over scripted detections --------


def test_reducer_emits_a_surface_with_opaque_handles() -> None:
    # The model PROPOSES these; the reducer DISPOSES into a Surface.
    dets = [
        DetectedElement(role="textbox", label="Email", bbox=[10, 10, 200, 30], confidence=0.95),
        DetectedElement(role="button", label="Sign in", bbox=[10, 60, 100, 40], confidence=0.9),
    ]
    s = reduce_vision_surface(dets, url="https://blind.test/", viewport=(1280, 720))
    assert isinstance(s, Surface)
    assert [a.handle for a in s.affordances] == ["a1", "a2"]
    assert [a.label for a in s.affordances] == ["Email", "Sign in"]
    assert s.blind is False
    # The handle map is the harness-side click-point indirection (px stays here).
    assert s.handle_map["a1"]["point"] == [110.0, 25.0]  # bbox centre
    assert s.handle_map["a2"]["point"] == [60.0, 80.0]
    assert s.handle_map["a1"]["bbox"] == [10, 10, 200, 30]


def test_reducer_prunes_low_confidence_tiny_offscreen_and_occluded() -> None:
    dets = [
        DetectedElement(role="button", label="Real", bbox=[0, 0, 100, 40], confidence=0.9),
        DetectedElement(role="button", label="Unsure", bbox=[0, 0, 100, 40], confidence=0.1),
        DetectedElement(role="button", label="Speck", bbox=[0, 0, 2, 2], confidence=0.9),
        DetectedElement(role="button", label="OffScreen", bbox=[5000, 0, 50, 40], confidence=0.9),
        DetectedElement(role="button", label="Hidden", bbox=[0, 0, 50, 40], confidence=0.9, occluded=True),
    ]
    s = reduce_vision_surface(dets, viewport=(1280, 720))
    # Only the one real, perceptible control survives — pruning is on
    # PERCEPTIBILITY, never task relevance.
    assert [a.label for a in s.affordances] == ["Real"]


def test_reducer_does_not_reorder_by_task_relevance() -> None:
    # Even with a "Place order" that a task-aware ranker would float to the top,
    # handles follow DETECTION ORDER — enumerate the possible, never choose.
    dets = [
        DetectedElement(role="link", label="Help", bbox=[0, 0, 50, 20], confidence=0.9),
        DetectedElement(role="button", label="Place order", bbox=[0, 30, 120, 40], confidence=0.9),
        DetectedElement(role="link", label="Cancel", bbox=[0, 80, 60, 20], confidence=0.9),
    ]
    s = reduce_vision_surface(dets)
    assert [(a.handle, a.label) for a in s.affordances] == [
        ("a1", "Help"),
        ("a2", "Place order"),
        ("a3", "Cancel"),
    ]


def test_text_detections_become_context_not_affordances() -> None:
    dets = [
        DetectedElement(role="text", label="Welcome back", bbox=[0, 0, 200, 20], confidence=0.9),
        DetectedElement(role="button", label="Continue", bbox=[0, 30, 100, 40], confidence=0.9),
    ]
    s = reduce_vision_surface(dets)
    assert s.context == ["Welcome back"]
    assert [a.label for a in s.affordances] == ["Continue"]


def test_blind_when_all_detections_below_floor() -> None:
    dets = [
        DetectedElement(role="button", label="Maybe", bbox=[0, 0, 100, 40], confidence=0.1),
        DetectedElement(role="button", label="Perhaps", bbox=[0, 0, 100, 40], confidence=0.2),
    ]
    s = reduce_vision_surface(dets)
    assert s.affordances == []
    assert s.blind is True
    assert s.blind_reason and "perceptibility floor" in s.blind_reason


def test_blind_when_detections_yield_only_context() -> None:
    dets = [DetectedElement(role="text", label="A heading", bbox=[0, 0, 200, 20], confidence=0.9)]
    s = reduce_vision_surface(dets)
    assert s.affordances == []
    assert s.blind is True


def test_blind_when_too_many_controls_unlabeled() -> None:
    dets = [
        DetectedElement(role="button", label="OK", bbox=[0, 0, 50, 30], confidence=0.9),
        DetectedElement(role="button", label="", bbox=[0, 40, 50, 30], confidence=0.9),
        DetectedElement(role="button", label="", bbox=[0, 80, 50, 30], confidence=0.9),
    ]
    s = reduce_vision_surface(dets)  # 2/3 unlabeled > 0.5
    assert s.blind is True
    assert s.blind_reason and "unlabeled" in s.blind_reason


def test_empty_detections_is_not_blind_no_content() -> None:
    # No detections at all is NOT the blind signal here — blind means "had content
    # but could not surface it". An empty page is honestly empty.
    s = reduce_vision_surface([])
    assert s.affordances == []
    assert s.blind is False


# --- the tool: capture → inject detector → reduce; handle resolve/escalate ----


def _fake_detector(dets: list[DetectedElement]) -> VisionDetector:
    def detect(image: Image) -> list[DetectedElement]:
        assert isinstance(image, Image)  # the detector really sees the screenshot
        return list(dets)

    return detect


class _FakeShotSession:
    def __init__(self, png: bytes) -> None:
        self._png = png

    async def send(self, cmd: dict) -> dict:
        assert cmd["op"] == "screenshot"
        return {
            "screenshot_b64": base64.b64encode(self._png).decode("ascii"),
            "mime": "image/png", "width": 1280, "height": 720, "url": "https://blind.test/",
        }

    async def close(self) -> None:  # pragma: no cover - not used
        pass


class _Ctx:
    def __init__(self, task_id: str) -> None:
        self.spec = type("S", (), {"task_id": task_id})()


async def test_op_surface_reduces_to_a_real_vision_surface() -> None:
    dets = [
        DetectedElement(role="textbox", label="Email", bbox=[10, 10, 200, 30], confidence=0.95),
        DetectedElement(role="button", label="Sign in", bbox=[10, 60, 100, 40], confidence=0.9),
    ]
    tool = VisionCapture(session=_FakeShotSession(_PNG), detector=_fake_detector(dets))
    out = await tool(None, op="surface")
    vs = out["vision_surface"]
    assert out["surface_blind"] is False
    assert [a["handle"] for a in vs["affordances"]] == ["a1", "a2"]
    # handle_map is harness-side and must NOT cross into the model-visible obs.
    assert "handle_map" not in vs
    # the pixels still ride along for a VLM policy.
    assert Image.model_validate(out["image"]).data == _PNG


async def test_op_surface_resolve_returns_click_point_and_stale_escalates() -> None:
    dets = [DetectedElement(role="button", label="Go", bbox=[10, 10, 100, 40], confidence=0.9)]
    tool = VisionCapture(session=_FakeShotSession(_PNG), detector=_fake_detector(dets))
    await tool(None, op="surface")
    # the pointer resolves a vision handle to its click-point (harness-side).
    res = await tool(None, op="resolve", handle="a1")
    assert res["locator"]["point"] == [60.0, 30.0]
    # a stale handle is an ESCALATION signal, never a crash.
    stale = await tool(None, op="resolve", handle="a99")
    assert stale["stale_handle"] == "a99"
    assert "error" in stale


async def test_op_surface_blind_with_no_detector_configured() -> None:
    # No model wired ⇒ honestly blind (last tier ⇒ escalate to a human), not a crash.
    tool = VisionCapture(session=_FakeShotSession(_PNG))  # no detector
    out = await tool(None, op="surface")
    assert out["surface_blind"] is True
    assert "no vision detector" in out["vision_surface"]["blind_reason"]


async def test_op_surface_stores_handle_map_in_run_registry_for_the_pointer() -> None:
    # The PRODUCTION cross-tool path: op=surface writes the handle→click-point map
    # into the run registry, so the pointer (a different tool instance) resolves the
    # same handle the model emitted.
    from zu_tools import _session

    dets = [DetectedElement(role="button", label="Buy", bbox=[20, 20, 80, 40], confidence=0.9)]
    ctx = _Ctx("run-vision")
    with _session._LOCK:
        _session._RUNS["run-vision"] = _session._RunEntry(handle=_FakeShotSession(_PNG))
    tool = VisionCapture(detector=_fake_detector(dets))  # attaches to the run session
    await tool(ctx, op="surface")
    loc = _session.resolve_handle("run-vision", "a1")
    assert loc is not None and loc["point"] == [60.0, 40.0]
    with _session._LOCK:
        _session._RUNS.pop("run-vision", None)


async def test_op_capture_still_thin_and_unchanged() -> None:
    # The capture step is preserved exactly: raw pixels, no detection.
    tool = VisionCapture(session=_FakeShotSession(_PNG))
    out = await tool(None, op="capture")
    assert out["vision"]["url"] == "https://blind.test/"
    assert "vision_surface" not in out
    assert Image.model_validate(out["image"]).data == _PNG


# --- the MODALITY-AGNOSTIC proof (§4.4/§4.5) ----------------------------------


def test_vision_surface_is_shape_identical_to_a11y_surface() -> None:
    """Same detections-vs-axnodes content ⇒ the SAME core SurfaceView shape. The
    vision producer and the a11y producer are interchangeable: same fields, same
    handle assignment, same projection — so everything downstream is unchanged."""
    from zu_tools.action_surface import AxNode

    vision = to_surface_view(
        reduce_vision_surface(
            [
                DetectedElement(role="textbox", label="Email", bbox=[0, 0, 200, 30], confidence=0.9),
                DetectedElement(role="button", label="Sign in", bbox=[0, 40, 100, 40], confidence=0.9),
            ]
        )
    )
    a11y = to_surface_view(
        reduce_surface(
            [
                AxNode(role="textbox", name="Email", bounds=[0, 0, 200, 30]),
                AxNode(role="button", name="Sign in", bounds=[0, 40, 100, 40]),
            ]
        )
    )
    assert isinstance(vision, SurfaceView) and isinstance(a11y, SurfaceView)
    # identical affordance shape (role/label/handle) — the interface is agnostic.
    assert [(a.handle, a.role, a.label) for a in vision.affordances] == \
           [(a.handle, a.role, a.label) for a in a11y.affordances]
    # same TYPE — the two producers emit one currency, fully interchangeable.
    assert type(vision) is type(a11y) is SurfaceView


def test_recognizer_matches_an_archetype_over_a_vision_surface() -> None:
    """The MODALITY-AGNOSTIC proof in its strong form: the §5 recognizer (which
    takes a CORE SurfaceView and never imports zu-tools) recognizes the SAME
    archetype over a VISION-produced surface as it would over an a11y one. zu-tools
    PRODUCTION code never imports zu-patterns; only this test imports both leaves,
    which is a clean direction (no production cycle)."""
    from zu_patterns import recognize
    from zu_patterns.login_form import LoginForm

    # A login form, but DETECTED IN PIXELS by the fake vision detector.
    view = to_surface_view(
        reduce_vision_surface(
            [
                DetectedElement(role="textbox", label="Email", bbox=[0, 0, 200, 30], confidence=0.9),
                DetectedElement(
                    role="textbox", label="Password", bbox=[0, 40, 200, 30],
                    confidence=0.9, states=["password"],
                ),
                DetectedElement(role="button", label="Sign in", bbox=[0, 80, 100, 40], confidence=0.9),
            ]
        )
    )
    rec = recognize(view, [LoginForm()])
    assert rec.result is not None
    assert rec.result.archetype == "login_form"
    # the recognized handles are the vision reducer's opaque handles.
    assert set(rec.result.matched_handles).issubset({"a1", "a2", "a3"})
