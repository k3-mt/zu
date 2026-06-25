"""vision — the tier-4 capture tool (Engineering Design §11.4).

The Action Surface (tier 3) signals ``blind`` on a canvas/icon page; the
``action-surface-blind`` detector ESCALATEs and the loop's ladder climbs to tier 4.
This proves the rung the climb lands on: a thin capture tool that screenshots the
SAME run-scoped page and hands the pixels to the policy as an Image content part —
no element detection (that is the vision MODEL of §6/Phase 3), and no Docker (a fake
session returns a fixed PNG).
"""

from __future__ import annotations

import base64

from zu_core.content import Image, Observation
from zu_tools.vision import VisionCapture

# a 1x1 transparent PNG
_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


class _FakeShotSession:
    def __init__(self, png: bytes) -> None:
        self._png = png
        self.sent: list[dict] = []

    async def send(self, cmd: dict) -> dict:
        self.sent.append(cmd)
        assert cmd["op"] == "screenshot"
        return {
            "screenshot_b64": base64.b64encode(self._png).decode("ascii"),
            "mime": "image/png", "width": 1280, "height": 720, "url": "https://blind.test/",
        }

    async def close(self) -> None:  # pragma: no cover - not used
        pass


async def test_capture_returns_an_image_content_part() -> None:
    session = _FakeShotSession(_PNG)
    out = await VisionCapture(session=session)(None, op="capture")
    assert out["vision"]["url"] == "https://blind.test/"
    assert out["vision"]["width"] == 1280
    # the image round-trips into a zu_core Image the policy reads off an Observation
    img = Image.model_validate(out["image"])
    assert img.data == _PNG and img.mime == "image/png"
    obs = Observation(content=[img])
    assert obs.parts("image") == [img]
    assert session.sent[0]["op"] == "screenshot"


async def test_capture_passes_full_page_flag() -> None:
    session = _FakeShotSession(_PNG)
    await VisionCapture(session=session)(None, op="capture", full_page=True)
    assert session.sent[0]["full_page"] is True


class _Ctx:
    def __init__(self, task_id: str) -> None:
        self.spec = type("S", (), {"task_id": task_id})()


async def test_capture_without_session_errors_not_crashes() -> None:
    out = await VisionCapture()(_Ctx("run-none"), op="capture")
    # nothing open for this run and no injected session -> a clean error, not a crash
    assert "open browser session" in out["error"]


async def test_capture_attaches_to_the_run_scoped_session_no_injection() -> None:
    # The PRODUCTION path: vision builds no backend and is injected no session; it
    # ATTACHES to the run's shared session (the page action_surface was blind on)
    # purely via the module registry, keyed by ctx.spec.task_id.
    from zu_tools import _session

    session = _FakeShotSession(_PNG)
    with _session._LOCK:
        _session._RUNS["run-blind"] = _session._RunEntry(handle=session)
    out = await VisionCapture()(_Ctx("run-blind"), op="capture")
    assert out["vision"]["url"] == "https://blind.test/"
    assert session.sent[0]["op"] == "screenshot"


async def test_bad_screenshot_response_is_an_error() -> None:
    class _Broken:
        async def send(self, cmd: dict) -> dict:
            return {"error": "page gone"}

        async def close(self) -> None:  # pragma: no cover - not used
            pass

    out = await VisionCapture(session=_Broken())(None, op="capture")
    assert "could not capture screenshot" in out["error"]


# --- the escalation ladder reaches tier 4 -----------------------------------


def test_blind_escalation_ladder_reaches_vision_at_tier_4() -> None:
    # The loop's _Ladder is the climb mechanism; with a tier-3 surface and the
    # tier-4 vision tool registered, an ESCALATE climbs to tier 4 and now OFFERS
    # vision — the rung that previously did not exist.
    from zu_core.loop import _Ladder

    class _T3:
        name = "action_surface"
        tier = 3
        schema = {"name": "action_surface"}

    tools = {"action_surface": _T3(), "vision": VisionCapture()}
    ladder = _Ladder(tools, max_tier=4)
    assert ladder.ceiling == 4                        # vision gives the ladder a tier-4 rung
    assert "vision" not in ladder.active()            # not offered below tier 4
    while ladder.can_climb:
        ladder.climb()
    assert ladder.current == 4
    assert "vision" in ladder.active()                # the blind escalation can now reach it
