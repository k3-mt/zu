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
    # the handle map is HARNESS-SIDE and must NOT leak into the model-visible obs
    assert "handle_map" not in out
    assert "handle_map" not in out["action_surface"]
    # nor any raw locator/selector for an affordance the model sees
    for aff in out["action_surface"]["affordances"]:
        assert "role" in aff and "name" not in aff  # role is shown; the {role,name} locator is not


async def test_tool_resolve_and_stale_handle() -> None:
    tool = ActionSurface()
    await tool(None, op="reduce", nodes=[n.model_dump() for n in _checkout_tree()])
    good = await tool(None, op="resolve", handle="a3")
    assert good["locator"] == {"role": "button", "name": "Place order"}
    stale = await tool(None, op="resolve", handle="a99")
    assert stale["stale_handle"] == "a99" and "error" in stale


class _FakeAxSession:
    def __init__(
        self, axtree: list[dict], *, html: str | None = None, status: int | None = None
    ) -> None:
        self._axtree = axtree
        self._html = html
        self._status = status
        self.closed = False
        self.last_cmd: dict | None = None

    async def send(self, cmd: dict) -> dict:
        assert cmd["op"] == "axtree"
        self.last_cmd = cmd
        resp: dict = {"axtree": self._axtree, "title": "Live", "url": cmd["url"]}
        # A live browser server echoes the raw markup + navigation status when asked
        # (issue #40). A fake without them stays valid — the tool returns None.
        if self._html is not None:
            resp["html"] = self._html
        if self._status is not None:
            resp["status"] = self._status
        return resp

    async def close(self) -> None:
        self.closed = True


class _FakeAxBackend:
    def __init__(
        self, axtree: list[dict], *, html: str | None = None, status: int | None = None
    ) -> None:
        self._axtree = axtree
        self._html = html
        self._status = status
        self.sessions: list[_FakeAxSession] = []

    async def open_session(self, spec: dict) -> _FakeAxSession:
        s = _FakeAxSession(self._axtree, html=self._html, status=self._status)
        self.sessions.append(s)
        return s


async def test_tool_open_op_captures_and_reduces_live_tree() -> None:
    cdp = [
        {"role": {"value": "button"}, "name": {"value": "Buy"}, "ignored": False},
        {"role": {"value": "link"}, "name": {"value": "Home"}, "ignored": False},
    ]
    backend = _FakeAxBackend(cdp)
    tool = ActionSurface(backend=backend, allow_private=True)
    out = await tool(_AxCtx("run-open"), op="open", url="http://shop.test/")
    assert [a["label"] for a in out["action_surface"]["affordances"]] == ["Buy", "Home"]
    assert out["action_surface"]["url"] == "http://shop.test/"
    assert "handle_map" not in out  # harness-side only
    # The AUTHORITATIVE run-end teardown closes the shared session (not the tool's
    # aclose, which only drops its reference).
    # Additive fields are present even when the session does not supply them: html is
    # None, http_status is None (issue #40 backward compatibility).
    assert "html" in out and out["html"] is None
    assert "http_status" in out and out["http_status"] is None
    from zu_tools._session import close_run
    await close_run("run-open")
    assert backend.sessions[0].closed


async def test_open_op_returns_html_and_http_status_when_the_session_supplies_them() -> None:
    """The live read now carries ``html`` (raw DOM) and ``http_status`` (the last
    navigation's HTTP status) alongside affordances/text/title, mirroring the tier-1
    fetch/render ``{status, html}`` shape — so a status>=400 check and an
    iframe/script-src scan run on the interactive arm (issue #40)."""
    cdp = [{"role": {"value": "button"}, "name": {"value": "Buy"}, "ignored": False}]
    markup = '<html><body><iframe src="//widget.test/x"></iframe>Not Found</body></html>'
    backend = _FakeAxBackend(cdp, html=markup, status=404)
    tool = ActionSurface(backend=backend, allow_private=True)
    out = await tool(_AxCtx("run-html"), op="open", url="http://shop.test/missing")
    # The additive signals a detector consumes are now on the interactive arm.
    assert out["html"] == markup
    assert out["http_status"] == 404
    # And the tool actually asked the session for the markup (op=axtree, html=True).
    assert backend.sessions[0].last_cmd == {
        "op": "axtree", "url": "http://shop.test/missing", "html": True,
    }
    from zu_tools._session import close_run
    await close_run("run-html")


class _RunScopedAxBackend:
    """A fake backend exposing open_run_session (the run-scoped lease) so the Action
    Surface's shared-session path is exercised: a ctx with a task_id routes through
    open_run_session keyed by it, the cross-tool sharing seam."""

    def __init__(self, axtree: list[dict]) -> None:
        self._axtree = axtree
        self.run_keys: list[str] = []
        self._sessions: dict = {}

    async def open_session(self, spec: dict) -> _FakeAxSession:
        return _FakeAxSession(self._axtree)

    async def open_run_session(self, spec: dict, *, run_key: str) -> _FakeAxSession:
        self.run_keys.append(run_key)
        s = _FakeAxSession(self._axtree)
        self._sessions[run_key] = s
        return s


class _AxCtx:
    def __init__(self, task_id: str) -> None:
        self.spec = type("S", (), {"task_id": task_id})()


async def test_open_op_uses_run_scoped_session_when_ctx_has_a_task_id() -> None:
    cdp = [{"role": {"value": "button"}, "name": {"value": "Buy"}, "ignored": False}]
    backend = _RunScopedAxBackend(cdp)
    tool = ActionSurface(backend=backend, allow_private=True)
    out = await tool(_AxCtx("run-7"), op="open", url="http://shop.test/")
    assert [a["label"] for a in out["action_surface"]["affordances"]] == ["Buy"]
    assert backend.run_keys == ["run-7"]            # leased under the run key for sharing


def test_blind_detector_escalates_on_blind_surface() -> None:
    det = ActionSurfaceBlindDetector()
    obs = {"surface_blind": True, "action_surface": {"blind_reason": "tree too thin"}}
    verdict = det.inspect(RunContext(spec=None, observation=obs))
    assert verdict is not None
    assert verdict.severity is Severity.ESCALATE
    assert "thin" in (verdict.detail or "")


def test_blind_detector_reads_vision_surface_and_words_for_the_last_tier() -> None:
    # When the VISION surface (the last perception tier) is blind it emits
    # `vision_surface`, not `action_surface`. The detector must read that key for the
    # reason and word the escalation as "to a human" — there is no tier-5.
    det = ActionSurfaceBlindDetector()
    obs = {"surface_blind": True,
           "vision_surface": {"blind_reason": "no detector cleared the perceptibility floor"}}
    verdict = det.inspect(RunContext(spec=None, observation=obs))
    assert verdict is not None and verdict.severity is Severity.ESCALATE
    assert "perceptibility floor" in (verdict.detail or "")
    # A vision-blind with no explicit reason still escalates to a human, not "to vision".
    bare = det.inspect(RunContext(spec=None, observation={"surface_blind": True,
                                                          "vision_surface": {}}))
    assert bare is not None and "human" in (bare.detail or "")
    assert "to vision" not in (bare.detail or "")


def test_blind_detector_silent_on_good_surface() -> None:
    det = ActionSurfaceBlindDetector()
    assert det.inspect(RunContext(spec=None, observation={"surface_blind": False})) is None
    assert det.inspect(RunContext(spec=None, observation={"text": "a page"})) is None


def test_unnamed_select_dropped_by_default_kept_via_keep_unnamed_roles() -> None:
    # A variant <select> routinely has NO accessible name; its options are the
    # signal (#110). The default reduction drops it (name-based tool/pointer path);
    # opting the role in keeps it, using its current value as the fallback label.
    nodes = [
        AxNode(role="combobox", name="", value="Choose an option", node_id=7),
        AxNode(role="button", name="Add to basket"),
    ]
    dropped = reduce_surface(nodes)
    assert [a.role for a in dropped.affordances] == ["button"]  # select dropped by default

    kept = reduce_surface(nodes, keep_unnamed_roles=frozenset({"combobox"}))
    combos = [a for a in kept.affordances if a.role == "combobox"]
    assert len(combos) == 1
    assert combos[0].label == "Choose an option"           # fell back to its value
    assert kept.handle_map[combos[0].handle]["node_id"] == 7  # still addressable by node id
    assert not kept.blind                                    # a kept select is not blindness


def _ax(node_id: str, role: str, name: str = "", *, children: list[str] | None = None,
        backend: int | None = None) -> dict:
    n: dict = {"nodeId": node_id, "role": {"value": role}, "name": {"value": name},
               "childIds": children or []}
    if backend is not None:
        n["backendDOMNodeId"] = backend
    return n


def test_normalize_stamps_group_id_from_the_enclosing_container() -> None:
    # Two radiogroups (colour, size); each option gets its container's group id, so a
    # flat list can tell colour swatches from size swatches (#120).
    nodes = [
        _ax("root", "RootWebArea", children=["cg", "sg"]),
        _ax("cg", "radiogroup", children=["c1", "c2"]),
        _ax("c1", "radio", "Red", backend=11),
        _ax("c2", "radio", "Green", backend=12),
        _ax("sg", "radiogroup", children=["s1", "s2"]),
        _ax("s1", "radio", "Small", backend=21),
        _ax("s2", "radio", "Large", backend=22),
    ]
    surface = reduce_surface(normalize_axtree(nodes))
    groups = {a.label: a.group for a in surface.affordances}
    assert groups == {"Red": "g:cg", "Green": "g:cg", "Small": "g:sg", "Large": "g:sg"}


def test_normalize_group_is_none_without_a_container() -> None:
    nodes = [_ax("root", "RootWebArea", children=["r1"]), _ax("r1", "radio", "Lonely", backend=1)]
    surface = reduce_surface(normalize_axtree(nodes))
    assert surface.affordances[0].group is None


def test_normalize_stamps_enclosing_label_from_card_heading() -> None:
    # A list of service cards whose buttons are ALL named 'Select'; each button must
    # carry its own card heading as the enclosing label so a hint can disambiguate (#127).
    nodes = [
        _ax("root", "RootWebArea", children=["list"]),
        _ax("list", "list", children=["c1", "c2"]),
        _ax("c1", "listitem", children=["h1", "b1"]),
        _ax("h1", "heading", "Cut & Finish"),
        _ax("b1", "button", "Select", backend=11),
        _ax("c2", "listitem", children=["h2", "b2"]),
        _ax("h2", "heading", "Full Head Colour"),
        _ax("b2", "button", "Select", backend=12),
    ]
    surface = reduce_surface(normalize_axtree(nodes))
    selects = sorted((a.enclosing_label or "") for a in surface.affordances if a.label == "Select")
    assert selects == ["Cut & Finish", "Full Head Colour"]


def test_enclosing_label_prefers_container_own_name_over_heading() -> None:
    nodes = [
        _ax("root", "RootWebArea", children=["g"]),
        _ax("g", "group", "Colour", children=["h", "r1"]),  # group has its own name
        _ax("h", "heading", "Ignored heading"),
        _ax("r1", "radio", "Red", backend=1),
    ]
    surface = reduce_surface(normalize_axtree(nodes))
    assert surface.affordances[0].enclosing_label == "Colour"


def test_enclosing_label_is_none_without_a_labelled_container() -> None:
    nodes = [_ax("root", "RootWebArea", children=["b"]), _ax("b", "button", "Go", backend=1)]
    surface = reduce_surface(normalize_axtree(nodes))
    assert surface.affordances[0].enclosing_label is None
