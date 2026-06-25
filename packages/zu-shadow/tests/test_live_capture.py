"""The live headed capture's pure translation + intent attachment, tested offline.

The headed drive (launch Chrome, the human clicks, a "why?" pops up at forks) is manual,
but the payload->RawInput mapping and the intent-attachment it relies on are pure and are
the load-bearing contract: a captured action becomes the SAME semantic RawInput the
synthetic/offline path uses — {role,name,label}, never a selector/coordinate — and a
fork's typed "why" lands on that step's intent.
"""

from __future__ import annotations

from zu_shadow.live_capture import _attach_intent, _payload_to_raw


def test_click_payload_is_semantic() -> None:
    ri = _payload_to_raw({"kind": "click", "role": "button", "name": "Confirm booking"})
    assert ri is not None and ri.kind == "click"
    assert ri.target is not None
    assert (ri.target.role, ri.target.name, ri.target.label) == ("button", "Confirm booking", "Confirm booking")
    assert set(ri.target.model_dump()) == {"role", "name", "label"}  # no selector/coordinate


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


def test_unknown_payload_is_skipped() -> None:
    assert _payload_to_raw({"kind": "scroll"}) is None
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
