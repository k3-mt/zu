"""The live headed capture's pure translation + intent attachment, tested offline.

The headed drive (launch Chrome, the human clicks, a "why?" pops up at forks) is manual,
but the payload->RawInput mapping and the intent-attachment it relies on are pure and are
the load-bearing contract: a captured action becomes the SAME semantic RawInput the
synthetic/offline path uses — {role,name,label}, never a selector/coordinate — and a
fork's typed "why" lands on that step's intent.
"""

from __future__ import annotations

from zu_shadow.live_capture import _attach_intent, _payload_to_raw


def test_a11y_capture_js_and_choose_logic_are_single_sourced() -> None:
    # F13: the a11y-capture JS (clean/role/name) and the model-choose logic must be
    # extracted into ONE shared module/function imported by all the blobs, not copy-pasted.
    # This pins that the de-duplication holds: the shared fragment is composed into the
    # capture + enumerate blobs, and the shared choose halves are the ones both the async
    # executor and the sync live drive call. On the OLD (duplicated) code the shared
    # symbols did not exist, so this import fails.
    from zu_shadow import _page_js, executor, live_capture, live_executor

    frag = _page_js.A11Y_HELPERS_JS
    assert "function clean" in frag and "function role" in frag and "function name" in frag
    # Both in-page blobs are BUILT from the one shared fragment (not their own copies).
    assert frag in live_capture.CAPTURE_JS
    assert frag in live_executor._ENUMERATE_JS
    # The shared selector is used by the enumeration.
    assert _page_js.ACTIONABLE_SELECTOR in live_executor._ENUMERATE_JS
    # The model-choose seam is shared: the sync live chooser reuses the executor's halves.
    assert executor._choose_handle_request is not None and executor._pick_handle is not None
    src = __import__("inspect").getsource(live_executor._choose_sync)
    assert "_choose_handle_request" in src and "_pick_handle" in src


def test_shared_pick_handle_bounds_reply_to_real_handles() -> None:
    # The extracted _pick_handle is the ONE parser both paths use: it returns only a REAL
    # handle named in the reply, else None (escalate, never guess).
    from zu_shadow.executor import _pick_handle

    assert _pick_handle("I think a3 fits", {"a1", "a3"}) == "a3"
    assert _pick_handle("use z9", {"a1", "a3"}) is None
    assert _pick_handle("", {"a1"}) is None


def test_click_payload_is_semantic() -> None:
    ri = _payload_to_raw({"kind": "click", "role": "button", "name": "Confirm booking"})
    assert ri is not None and ri.kind == "click"
    assert ri.target is not None
    assert (ri.target.role, ri.target.name, ri.target.label) == ("button", "Confirm booking", "Confirm booking")
    # The target carries the semantic fields plus the locale-independent STRUCTURAL
    # signals (input type / autocomplete / submits) — but never a selector or coordinate.
    assert set(ri.target.model_dump()) == {"role", "name", "label",
                                           "input_type", "autocomplete", "submits"}
    assert "selector" not in ri.target.model_dump() and "x" not in ri.target.model_dump()


def test_click_carries_intent_when_tagged() -> None:
    ri = _payload_to_raw({"kind": "click", "role": "button", "name": "Chislehurst",
                          "intent": "the clinic nearest me"})
    assert ri is not None and ri.intent == "the clinic nearest me"


def test_type_payload_carries_value_and_role_fallback() -> None:
    ri = _payload_to_raw({"kind": "type", "name": "Your email", "value": "a@b.com"})
    assert ri is not None and ri.kind == "type" and ri.value == "a@b.com"
    assert ri.target is not None and ri.target.role == "textbox"  # default when role absent


def test_navigate_and_network_payloads() -> None:
    nav = _payload_to_raw({"kind": "navigate", "url": "https://x.example/book"})
    assert nav is not None and nav.kind == "navigate" and nav.url == "https://x.example/book"
    net = _payload_to_raw({"kind": "network", "url": "https://api.x.example/s",
                           "status": 200, "host": "api.x.example"})
    assert net is not None and net.kind == "network" and net.host == "api.x.example"


def test_scroll_payload_carries_direction_and_position() -> None:
    ri = _payload_to_raw({"kind": "scroll", "dir": "down", "y": 1200})
    assert ri is not None and ri.kind == "scroll" and ri.value == "down" and ri.status == 1200
    up = _payload_to_raw({"kind": "scroll", "dir": "up", "y": 0})
    assert up is not None and up.value == "up"


def test_unknown_payload_is_skipped() -> None:
    assert _payload_to_raw({"kind": "wheel"}) is None
    assert _payload_to_raw({}) is None


def test_attach_intent_targets_the_latest_unannotated_click() -> None:
    actions = [
        {"kind": "click", "name": "Chislehurst", "intent": "already explained"},
        {"kind": "navigate", "url": "https://x/after-click"},  # a nav the click triggered
        {"kind": "click", "name": "Confirm"},                  # the fork awaiting its why
    ]
    _attach_intent(actions, "the 2pm Saturday slot")
    assert actions[2]["intent"] == "the 2pm Saturday slot"   # the latest click, past the nav
    assert actions[0]["intent"] == "already explained"        # an earlier annotated click untouched


def test_attach_intent_is_a_noop_when_no_open_click() -> None:
    actions = [{"kind": "navigate", "url": "https://x"}]
    _attach_intent(actions, "orphan why")  # nothing to attach to — must not raise
    assert all("intent" not in a for a in actions)


async def _fold_actions(payloads: list[dict]):
    from zu_shadow.live_capture import _fold, _payload_to_raw
    items = [ri for a in payloads if (ri := _payload_to_raw(a)) is not None]
    return await _fold(items, site="https://x.example", outcome=None)


async def test_recording_doc_marks_complete_vs_truncated() -> None:
    # F14: a capture that failed mid-session must NOT be written as a clean success. The
    # persisted doc carries a ``complete`` flag so a reader/caller can tell a truncated
    # capture from a whole one — and CaptureIncomplete signals the failure. On the OLD
    # code the doc had no ``complete`` key and a partial recording was reported as success.
    from zu_shadow.live_capture import _count_steps, _recording_doc

    session = await _fold_actions([
        {"kind": "navigate", "url": "https://x.example/book"},
        {"kind": "click", "role": "button", "name": "Continue"},
    ])
    clean = _recording_doc(session, complete=True)
    truncated = _recording_doc(session, complete=False)
    assert clean["complete"] is True
    assert truncated["complete"] is False   # the ONE bit that distinguishes them
    assert _count_steps(session) == 2       # a navigate + a click are action steps


def test_capture_incomplete_carries_step_count_and_path() -> None:
    # The failure signal the caller sees: a distinct error type carrying how many steps
    # were captured before the failure and where the partial recording was written — so a
    # truncated capture is never silently mistaken for a clean one (F14).
    from zu_shadow.live_capture import CaptureIncomplete

    err = CaptureIncomplete("boom", steps=3, out="/tmp/rec.json")
    assert isinstance(err, RuntimeError)  # propagatable
    assert err.steps == 3 and err.out == "/tmp/rec.json"
