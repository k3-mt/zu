"""action_surface — the perception-reduction tool (Engineering Design §11).

The reducer is the value and is pure, so the bulk of this proves it on
accessibility-tree snapshots with no browser: the checkout example collapses to
a handful of handled affordances, the invisible is pruned, the model never sees
a selector (handles map back harness-side), and the competence boundary fires a
`blind` signal rather than silently returning an incomplete surface. The live
arm is exercised against a fake session that returns a CDP tree.
"""

from __future__ import annotations

from zu_checks.detectors.action_surface_blind import ActionSurfaceBlindDetector
from zu_core.ports import RunContext, Severity
from zu_tools.action_surface import (
    ActionSurface,
    AxNode,
    normalize_axtree,
    reduce_surface,
)


def _checkout_tree() -> list[AxNode]:
    """The §11.2 worked example as an accessibility tree."""
    return [
        AxNode(role="heading", name="Checkout — Acme"),
        AxNode(role="textbox", name="Discount code", placeholder="Discount code"),
        AxNode(role="button", name="Apply"),
        AxNode(role="button", name="Place order"),
        AxNode(role="link", name="Continue shopping"),
        AxNode(role="combobox", name="Shipping method", value="Standard"),
    ]


def test_checkout_reduces_to_handled_affordances() -> None:
    s = reduce_surface(_checkout_tree(), title="Checkout — Acme", url="/cart")
    # five affordances, handles in document order, the heading kept as context.
    assert [a.handle for a in s.affordances] == ["a1", "a2", "a3", "a4", "a5"]
    labels = {a.handle: a.label for a in s.affordances}
    assert labels == {
        "a1": "Discount code",
        "a2": "Apply",
        "a3": "Place order",
        "a4": "Continue shopping",
        "a5": "Shipping method",
    }
    assert "Checkout — Acme" in s.context
    # the combobox's current value is surfaced separately from its label
    assert next(a for a in s.affordances if a.handle == "a5").value == "Standard"
    assert not s.blind


def test_handle_maps_back_to_role_and_name_not_a_selector() -> None:
    s = reduce_surface(_checkout_tree())
    assert s.handle_map["a3"] == {"role": "button", "name": "Place order"}
    # no CSS/selector currency anywhere in the surface the model sees
    assert all("name" in loc and "role" in loc for loc in s.handle_map.values())


def test_prune_invisible_and_zero_area() -> None:
    nodes = [
        AxNode(role="button", name="Visible"),
        AxNode(role="button", name="Hidden", visible=False),
        AxNode(role="button", name="Ignored", ignored=True),
        AxNode(role="button", name="ZeroArea", bounds=[0, 0, 0, 10]),
    ]
    s = reduce_surface(nodes)
    assert [a.label for a in s.affordances] == ["Visible"]


def test_blind_when_page_has_content_but_no_affordances() -> None:
    # A canvas-drawn page: nodes exist, none are addressable actions.
    nodes = [AxNode(role="generic", name="canvas app"), AxNode(role="img", name="")]
    s = reduce_surface(nodes)
    assert s.blind and s.blind_reason and "no addressable actions" in s.blind_reason


def test_blind_when_too_many_interactive_elements_are_unlabeled() -> None:
    nodes = [
        AxNode(role="button", name="OK"),
        AxNode(role="button", name=""),   # unlabeled icon
        AxNode(role="button", name=""),   # unlabeled icon
    ]
    s = reduce_surface(nodes, unlabeled_ratio=0.5)
    # 2/3 unlabeled > 0.5 → blind, but the one good affordance is still returned.
    assert s.blind
    assert [a.label for a in s.affordances] == ["OK"]


def test_not_blind_when_mostly_labeled() -> None:
    nodes = [AxNode(role="button", name="A"), AxNode(role="button", name="B"),
             AxNode(role="button", name="")]
    s = reduce_surface(nodes, unlabeled_ratio=0.5)
    assert not s.blind  # 1/3 unlabeled, under threshold


def test_normalize_cdp_axtree() -> None:
    cdp = [
        {
            "role": {"type": "role", "value": "button"},
            "name": {"type": "computedString", "value": "Submit"},
            "properties": [
                {"name": "disabled", "value": {"type": "boolean", "value": True}},
                {"name": "focusable", "value": {"type": "boolean", "value": True}},
            ],
            "ignored": False,
        },
        {"role": {"value": "textbox"}, "name": {"value": "Email"},
         "properties": [{"name": "required", "value": {"value": True}}], "ignored": False},
        {"role": {"value": "img"}, "name": {"value": "x"}, "ignored": True},
    ]
    nodes = normalize_axtree(cdp)
    assert [n.role for n in nodes] == ["button", "textbox", "img"]
    assert "disabled" in nodes[0].states
    assert "required" in nodes[1].states
    assert nodes[2].ignored

    s = reduce_surface(nodes)
    # the ignored img is pruned; button + textbox surface with their states
    assert [a.label for a in s.affordances] == ["Submit", "Email"]
    assert "disabled" in s.affordances[0].states


async def test_tool_reduce_op_with_nodes() -> None:
    tool = ActionSurface()
    out = await tool(None, op="reduce",
                     nodes=[n.model_dump() for n in _checkout_tree()],
                     title="Checkout — Acme", url="/cart")
    assert out["surface_blind"] is False
    assert len(out["action_surface"]["affordances"]) == 5
    # the handle map is held on the instance and echoed
    assert out["handle_map"]["a3"]["name"] == "Place order"


async def test_tool_resolve_and_stale_handle() -> None:
    tool = ActionSurface()
    await tool(None, op="reduce", nodes=[n.model_dump() for n in _checkout_tree()])
    good = await tool(None, op="resolve", handle="a3")
    assert good["locator"] == {"role": "button", "name": "Place order"}
    stale = await tool(None, op="resolve", handle="a99")
    assert stale["stale_handle"] == "a99" and "error" in stale


class _FakeAxSession:
    def __init__(self, axtree: list[dict]) -> None:
        self._axtree = axtree
        self.closed = False

    async def send(self, cmd: dict) -> dict:
        assert cmd["op"] == "axtree"
        return {"axtree": self._axtree, "title": "Live", "url": cmd["url"]}

    async def close(self) -> None:
        self.closed = True


class _FakeAxBackend:
    def __init__(self, axtree: list[dict]) -> None:
        self._axtree = axtree
        self.sessions: list[_FakeAxSession] = []

    async def open_session(self, spec: dict) -> _FakeAxSession:
        s = _FakeAxSession(self._axtree)
        self.sessions.append(s)
        return s


async def test_tool_open_op_captures_and_reduces_live_tree() -> None:
    cdp = [
        {"role": {"value": "button"}, "name": {"value": "Buy"}, "ignored": False},
        {"role": {"value": "link"}, "name": {"value": "Home"}, "ignored": False},
    ]
    backend = _FakeAxBackend(cdp)
    tool = ActionSurface(backend=backend, allow_private=True)
    out = await tool(None, op="open", url="http://shop.test/")
    assert [a["label"] for a in out["action_surface"]["affordances"]] == ["Buy", "Home"]
    assert out["action_surface"]["url"] == "http://shop.test/"
    await tool.aclose()
    assert backend.sessions[0].closed


def test_blind_detector_escalates_on_blind_surface() -> None:
    det = ActionSurfaceBlindDetector()
    obs = {"surface_blind": True, "action_surface": {"blind_reason": "tree too thin"}}
    verdict = det.inspect(RunContext(spec=None, observation=obs))
    assert verdict is not None
    assert verdict.severity is Severity.ESCALATE
    assert "thin" in (verdict.detail or "")


def test_blind_detector_silent_on_good_surface() -> None:
    det = ActionSurfaceBlindDetector()
    assert det.inspect(RunContext(spec=None, observation={"surface_blind": False})) is None
    assert det.inspect(RunContext(spec=None, observation={"text": "a page"})) is None
